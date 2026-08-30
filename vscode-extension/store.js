/**
 * アカウントと取得結果の置き場。
 *
 * ビルド工程を持たない素の CommonJS。require するのは 'vscode' と Node 組み込みだけで、
 * node_modules は使わない。**このファイルがそのまま vsix に入る原本です。**
 * コンパイル前後で中身が違うことがないので、配ったものを展開すれば原本が読めます。
 */

'use strict';

const vscode = require('vscode');

const { t } = require('./i18n');

/**
 * 取得の進み方。
 *
 *   unknown … まだ何もしていない
 *   queued  … 更新の列に入れた (バックエンドは1件ずつ処理する)
 *   loading … 実際に取得が始まった
 *   ok      … 取得できた
 *   error   … 取得に失敗した
 *
 * @typedef {'unknown'|'queued'|'loading'|'ok'|'error'} EntryState
 */

/**
 * 取得先が返した指標ひとつ (5時間枠・週間枠・当月コスト …)。
 *
 * **level と dot はバックエンド (Python) が入れたものです。** しきい値の判定は
 * services/usage_status.py にしかありません。ここで判定し直さないでください。
 *
 * @typedef {object} Metric
 * @property {string} [key]
 * @property {string} [label]
 * @property {string} [kind] percent / money / amount。表示方法の切り替えに使う
 * @property {number|null} [utilization] 消費率 (0-100)。ゲージに出せないときは null
 * @property {string} [display] 金額や「200 / 1,000」など、そのまま出せる文字列
 * @property {string|null} [resets_at] 枠が戻る時刻 (ISO8601)。無い指標では null
 * @property {string} [level] ok / caution / limited / unknown (Python が判定)
 * @property {string} [dot] この枠ひとつぶんの丸印 (Python が判定)
 */

/**
 * アカウント1件の判定済みの状態 (Python が入れたもの)。
 *
 * @typedef {object} UsageStatus
 * @property {string} level ok / caution / limited / unknown
 * @property {string} dot 一覧に出す丸印
 * @property {string} summary 一覧に出す1行要約
 */

/**
 * 取得結果ひとまとまり。
 *
 * @typedef {object} Usage
 * @property {Metric[]} [metrics]
 * @property {number|null} [max_utilization] 最も逼迫している枠の消費率
 * @property {UsageStatus} [status] 判定済みの状態 (Python が入れたもの)
 */

/**
 * アカウント1件について、手元に持っている取得結果。
 *
 * @typedef {object} Entry
 * @property {EntryState} state
 * @property {Usage} [usage]
 * @property {string} [error]
 * @property {boolean} [authError] 資格情報が切れている (再ログインが要る)
 * @property {number} [fetchedAt]
 */

/**
 * 使用率のしきい値と丸印は、ここにはありません。
 *
 * 以前はこのファイルに CAUTION_PERCENT / LIMITED_PERCENT / statusDot があり、
 * **デスクトップ版 (ui/styles.py) と必ず同じ値にすること** という約束を
 * コメントで守ろうとしていました。同じ数値が media/main.js にもあったため、
 * 3箇所を人が揃え続ける形になっていました。
 *
 * いまは backend/services/usage_status.py が唯一の持ち主で、判定した結果
 * (段階・丸印・1行要約) が取得結果に載って届きます。**ここで色を決め直さないで
 * ください。** 同じアカウントが exe と VSCode で違う色に見えると、どちらが
 * 本当なのか分からなくなります。
 */

/**
 * アカウントと取得結果を持ち、更新を仕切ります。
 *
 * 画面 (Webview) とステータスバーの両方がここを見ます。**取得結果を画面側に
 * 置かないでください。** パネルを閉じるたびに結果が消え、ステータスバーが
 * 空になります。
 *
 * @implements {vscode.Disposable}
 */
class UsageStore {
    /** @param {import('./backend').Backend} backend */
    constructor(backend) {
        this.backend = backend;

        /** @type {import('./backend').Snapshot|undefined} */
        this._snapshot = undefined;
        /** @type {Map<string, Entry>} */
        this.cache = new Map();
        this.refreshing = false;
        /**
         * 進行中の reload()。相乗りさせるために持ちます (reload() の説明を参照)。
         * @type {Promise<void>|undefined}
         */
        this.reloading = undefined;
        this._onDidChange = new vscode.EventEmitter();
        this.onDidChange = this._onDidChange.event;

        /** @type {vscode.Disposable[]} */
        this.disposables = [];

        this.disposables.push(
            backend.onFetching((accountId) => {
                // キューから出て実際に走り始めたものだけを「取得中」にする。
                // 全部を同時に取得中に見せると、何件目で止まっているのか
                // 分からなくなる (バックエンドは1件ずつ順番に処理する)。
                this.markPending(accountId, 'loading');
            }),
        );
    }

    dispose() {
        this._onDidChange.dispose();
        for (const d of this.disposables) {
            d.dispose();
        }
    }

    get snapshot() {
        return this._snapshot;
    }

    /** @returns {import('./backend').AccountInfo[]} */
    get accounts() {
        return this._snapshot?.accounts ?? [];
    }

    get isRefreshing() {
        return this.refreshing;
    }

    /**
     * 顔ぶれが変わった可能性があるとき、消えたアカウントの結果を cache から捨てます。
     *
     * 残すと `entry()` や `worst()` が存在しないアカウントの古い結果を持ち出し、
     * 件数や表示が合わなくなります。reload() と deleteAccount() で同じ
     * 後始末が必要なので、ここへ切り出しました。
     */
    pruneMissingAccounts() {
        const alive = new Set(this.accounts.map((a) => a.id));
        for (const id of [...this.cache.keys()]) {
            if (!alive.has(id)) {
                this.cache.delete(id);
            }
        }
    }

    /**
     * 登録内容を書き換えます。
     *
     * **別ウィンドウは出しません。** バックエンドの中だけで終わります
     * (backend/cli.py の update_account)。ここを PySide6 製のログイン画面へ
     * 通していたために、あれが入っていない環境では編集すらできませんでした。
     *
     * 尋ねるべき警告が出たときは**保存されず**、その文面が返ります。
     * 呼び出し側が利用者に尋ね、confirmed を立てて呼び直してください。
     *
     * @param {import('./backend').AccountUpdate} changes
     * @returns {Promise<string[]>} 尋ねるべき警告 (空配列なら保存済み)
     */
    async updateAccount(changes) {
        const result = await this.backend.updateAccount(changes);
        if (result.confirm) {
            // **保存されていないので、一覧も差し替えません。**
            return result.confirm;
        }
        this._snapshot = result;
        // 取得に効く値を変えたなら、前回の結果はもう当てになりません。
        // **名前だけ変えたときに捨てないこと** — 直後の画面が「未取得」に
        // 戻り、変えた覚えのない表示が消えたように見えます。
        if (changes.credential !== undefined || changes.provider !== undefined
            || changes.extra !== undefined) {
            this.cache.delete(changes.accountId);
        }
        this._onDidChange.fire();
        return [];
    }

    /**
     * アカウントを1件作ります。**ログイン画面は開きません。**
     *
     * **これが唯一の道です。** 窓を開いて作る道はもうありません。
     *
     * @param {object} changes create_account に渡す内容
     * @returns {Promise<{accountId?: string, confirm?: string[]}>}
     */
    async createAccount(changes) {
        const result = await this.backend.createAccount(changes);
        if (result.confirm) {
            // **作られていないので、一覧も差し替えません。**
            return { confirm: result.confirm };
        }
        this._snapshot = result;
        this._onDidChange.fire();
        return { accountId: result.accountId };
    }

    /**
     * 選べる取得先の一覧を読みます。
     *
     * @param {string[]} [include] 一覧から取り下げたものも要るなら、その ID
     * @returns {Promise<import('./backend').ProviderInfo[]>}
     */
    async listProviders(include = []) {
        const result = await this.backend.listProviders(include);
        return result.providers ?? [];
    }

    /**
     * アカウントを1件消します。
     *
     * **別ウィンドウは出しません。** バックエンドの中だけで終わります
     * (backend/cli.py の do_delete_account)。ここを PySide6 製のログイン画面へ
     * 通していたために、あれが入っていない環境では削除すらできませんでした。
     *
     * @param {string} accountId
     * @returns {Promise<void>}
     */
    async deleteAccount(accountId) {
        this._snapshot = await this.backend.deleteAccount(accountId);
        this.pruneMissingAccounts();
        this._onDidChange.fire();
    }

    /**
     * 取得済みの結果を全部捨てます。
     *
     * **表示言語を変えたときに要ります。** 枠のラベルも状態の要約も
     * エラー文も、訳したのはバックエンドです (cli.py が
     * services/usage_status.py の annotate を通して返す)。こちらに
     * 残っているのは前の言語で作られた文字列なので、捨てないと
     * **画面の枠だけ新しい言語になり、中身は前の言語のまま**残ります。
     */
    forgetUsage() {
        this.cache.clear();
        this._onDidChange.fire();
    }

    /**
     * @param {string} accountId
     * @returns {Entry}
     */
    entry(accountId) {
        return this.cache.get(accountId) ?? { state: 'unknown' };
    }

    /**
     * @param {string} accountId
     * @param {Entry} entry
     */
    setEntry(accountId, entry) {
        this.cache.set(accountId, entry);
        this._onDidChange.fire();
    }

    /**
     * 「待機中」「取得中」の印を付けます。
     *
     * **前回の結果は消しません。** 消すと画面から枠 (バー) が無くなって
     * 行の高さが変わり、更新のたびに表全体が上下にずれます。1分間隔の
     * 自動更新では、これが延々と繰り返されて読めたものではありません。
     * 値は取得が終わってから差し替えます。
     *
     * @param {string} accountId
     * @param {EntryState} state
     */
    markPending(accountId, state) {
        const previous = this.cache.get(accountId);
        this.setEntry(accountId, { ...previous, state });
    }

    /**
     * 今この場で取得を試せるアカウント (判定はバックエンド側が持っています)。
     * @returns {import('./backend').AccountInfo[]}
     */
    fetchableAccounts() {
        const ids = new Set(this._snapshot?.fetchable ?? []);
        return this.accounts.filter((a) => ids.has(a.id));
    }

    /**
     * アカウント一覧を読み直します。
     *
     * **進行中のものがあれば、それに相乗りします (投げ直しません)。**
     * 起動直後の読み込み (extension.js の activate 末尾) と、「snapshot が
     * まだ無ければ読む」(パネルを開く・全件更新・対象を選ぶ・追加) は必ず
     * 重なります。ウィンドウの再読み込み直後は、この2つが数百ミリ秒と離れずに
     * 走るので、ほぼ毎回重なります。
     *
     * 素直に2回投げると実害が2つ出ます。**list_accounts が実際に2往復し**
     * (バックエンドは要求を順番に処理するので、2本目は1本目の完了を待たされ、
     * 画面が出るまでの時間がそのぶん伸びます)、**onDidChange が余分に1回
     * 起きて、同じ内容で画面がもう一度描き直されます。** どちらも、いちばん
     * 見られたくない「復元直後」に重なります。
     *
     * @param {boolean} [force] 進行中のものに相乗りせず、必ず投げ直す。
     *   **backend.restart() (= kill()) の直後だけ true にします。** kill() は
     *   待機中の要求を全部 reject するので、その直後に相乗りすると、たった今
     *   殺された要求の拒否をそのまま受け取ります。再起動したのに「アカウントを
     *   読み込めませんでした」と出る、という一番紛らわしい失敗になります。
     * @returns {Promise<void>}
     */
    async reload(force = false) {
        if (!force && this.reloading) {
            return this.reloading;
        }
        const running = this._reload();
        this.reloading = running;
        try {
            await running;
        } finally {
            // **自分より後に始まったものの記録は消しません。** 無条件に消すと、
            // 遅れて終わった前の読み込みが、始まったばかりの読み込みの記録を
            // 消して、二重に走らせてしまいます。
            if (this.reloading === running) {
                this.reloading = undefined;
            }
        }
    }

    /**
     * reload() の中身。
     *
     * **相乗りの判定をここに書かないでください。** 判定は reload() が持ちます。
     * ここは「1回ぶんの読み込み」だけを行い、失敗はそのまま呼び出し元へ返します。
     *
     * @returns {Promise<void>}
     */
    async _reload() {
        this._snapshot = await this.backend.listAccounts(true);
        this.pruneMissingAccounts();
        this._onDidChange.fire();
    }

    /**
     * @param {string} accountId
     * @param {boolean} enabled
     * @returns {Promise<void>}
     */
    async setEnabled(accountId, enabled) {
        this._snapshot = await this.backend.setEnabled(accountId, enabled);
        this._onDidChange.fire();
    }

    /**
     * 画面に出す並び順を保存します。
     *
     * **pruneMissingAccounts() は呼びません。** 顔ぶれは変わらないので、
     * 取得済みの結果を捨てる理由がありません (捨てると、並べ替えるたびに
     * 一覧のゲージが空になります)。
     *
     * @param {string[]} order accountId を並べたもの
     * @returns {Promise<void>}
     */
    async reorderAccounts(order) {
        this._snapshot = await this.backend.reorderAccounts(order);
        this._onDidChange.fire();
    }

    /**
     * @param {string} accountId
     * @returns {Promise<void>}
     */
    async refreshOne(accountId) {
        this.markPending(accountId, 'queued');
        try {
            const result = await this.backend.fetchUsage(accountId);
            this.setEntry(accountId, {
                state: 'ok', usage: result.usage, fetchedAt: Date.now(),
            });
        } catch (e) {
            const error = /** @type {import('./backend').BackendError} */ (e);
            this.setEntry(accountId, {
                state: 'error',
                error: error.message,
                authError: error.authError === true,
                fetchedAt: Date.now(),
            });
        }
    }

    /**
     * 取得できるものをすべて更新します。
     *
     * 要求はまとめて投げますが、**実際に走るのは1件ずつです** (バックエンドが
     * 順番に処理します)。同時に投げると、同じ取得先へ並行してリクエストが飛び、
     * レート制限に当たったときに何が原因か分からなくなります。
     *
     * @returns {Promise<void>}
     */
    async refreshAll() {
        if (this.refreshing) {
            return;
        }
        if (!this._snapshot) {
            await this.reload();
        }
        const targets = this.fetchableAccounts();
        if (targets.length === 0) {
            return;
        }

        this.refreshing = true;
        for (const account of targets) {
            // ここでも前回の結果は残す (markPending と同じ理由)
            this.cache.set(account.id, { ...this.cache.get(account.id), state: 'queued' });
        }
        this._onDidChange.fire();

        try {
            await Promise.all(targets.map((a) => this.refreshOne(a.id)));
        } finally {
            this.refreshing = false;
            this._onDidChange.fire();
        }
    }

    /**
     * ステータスバーに出す「最も逼迫しているもの」。
     *
     * 取得できていないものは候補にしません。0% として扱うと「まだ余裕がある」
     * という嘘になります。
     *
     * **丸印はバックエンドが判定したものをそのまま持ち出します。** しきい値は
     * services/usage_status.py にしかないので、ここで色を決め直すことはしません。
     * 判定が載っていない結果 (protocol 1 以前のバックエンドを混ぜた場合) は
     * 未取得と同じ扱いにします。作り話の丸印を出すより、出さない方が正確です。
     *
     * @returns {{account: import('./backend').AccountInfo, utilization: number, dot: string}|undefined}
     */
    worst() {
        /** @type {{account: import('./backend').AccountInfo, utilization: number, dot: string}|undefined} */
        let found;
        for (const account of this.accounts) {
            const entry = this.cache.get(account.id);
            const utilization = entry?.usage?.max_utilization;
            const status = entry?.usage?.status;
            if (entry?.state !== 'ok' || utilization === null || utilization === undefined
                || !status) {
                continue;
            }
            if (!found || utilization > found.utilization) {
                found = { account, utilization, dot: status.dot };
            }
        }
        return found;
    }
}

module.exports = { UsageStore };
