/**
 * アカウントと取得結果の置き場。
 *
 * ビルド工程を持たない素の CommonJS。require するのは 'vscode' と Node 組み込みだけで、
 * node_modules は使わない。**このファイルがそのまま vsix に入る原本です。**
 * コンパイル前後で中身が違うことがないので、配ったものを展開すれば原本が読めます。
 */

'use strict';

const vscode = require('vscode');

const { ACCOUNT_OPERATIONS, t } = require('./i18n');

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
        /**
         * ログイン画面など、別ウィンドウでの操作を待っている間の説明。
         * @type {string|undefined}
         */
        this.guiBusyMessage = undefined;
        /**
         * 何回目の GUI 操作か。
         *
         * 中止したとき、待っていた操作の応答が返るより先に利用者が次の操作を
         * 始められます。番号を持たずに「終わったら undefined を書く」だけに
         * すると、遅れて返ってきた前の操作が、始まったばかりの操作の表示を
         * 消してしまいます。自分の番号のときだけ消すために持ちます。
         */
        this.guiOperationSeq = 0;

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

    get guiBusy() {
        return this.guiBusyMessage;
    }

    /**
     * 顔ぶれが変わった可能性があるとき、消えたアカウントの結果を cache から捨てます。
     *
     * 残すと `entry()` や `worst()` が存在しないアカウントの古い結果を持ち出し、
     * 件数や表示が合わなくなります。reload() と runGuiOperation()、
     * deleteAccount() で同じ後始末が必要なので、ここへ切り出しました。
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
     * ログイン画面を伴う操作を1つだけ走らせます。
     *
     * **同時に2つ開かせないこと。** 同じ設定ファイルを2つのダイアログが
     * 書き戻すことになり、後から閉じた方が先の変更を消します。
     *
     * @param {string} message
     * @param {() => Promise<import('./backend').Snapshot>} action
     * @returns {Promise<void>}
     */
    async runGuiOperation(message, action) {
        if (this.guiBusyMessage) {
            throw new Error(t('Another operation ({operation}) is in progress.',
                { operation: this.guiBusyMessage }));
        }
        const seq = ++this.guiOperationSeq;
        this.guiBusyMessage = message;
        this._onDidChange.fire();
        try {
            this._snapshot = await action();
            this.pruneMissingAccounts();
        } finally {
            // **自分より後に始まった操作の表示は消しません。** 中止したときは
            // cancelGui() が先に解除するので、応答が遅れて返ってくる頃には
            // 利用者が次の操作を始めていることがあります。ここで無条件に
            // undefined を書くと、その新しい操作だけが「進行中ではない」ことに
            // なり、画面のボタンが押せてしまいます。
            if (this.guiOperationSeq === seq) {
                this.guiBusyMessage = undefined;
            }
            this._onDidChange.fire();
        }
    }

    /**
     * 進行中のログイン画面を中止させます。
     *
     * **通知を閉じるだけの中止にはしないこと。** 実際に子プロセスを終了させ
     * なければ、固まったプロセスとバックエンド側の錠が残り、以後の操作が
     * すべて弾かれます。終了させるのはバックエンドの仕事です
     * (backend.cancelGui 参照)。
     *
     * @returns {Promise<{cancelled: boolean, message: string}>}
     */
    async cancelGui() {
        try {
            return await this.backend.cancelGui();
        } finally {
            // **待っている runGuiOperation の finally を当てにしません。**
            // 中止が要る場面は「何かが詰まっている」場面そのもので、応答が
            // 返ってこなければあの finally は永久に通りません。そうなると
            // 追加・編集・再ログインが「別の操作が進行中です」で弾かれ続け、
            // 復旧手段がウィンドウの再読み込みだけになります。二重に解除しても
            // 害はありません (同じ undefined を書くだけ)。ダイアログが二重に
            // 開くことは、バックエンド側の錠が引き続き防ぎます。
            this.guiBusyMessage = undefined;
            this._onDidChange.fire();
        }
    }

    // **guiBusyMessage には訳したものを入れます。** これは webview の案内文へ
    // そのまま差し込まれる値で、向こう側は訳す手立てを持ちません。
    // 原文 (キー) は i18n.ACCOUNT_OPERATIONS に1つだけ置いてあります。

    addAccount() {
        return this.runGuiOperation(
            t(ACCOUNT_OPERATIONS.add), () => this.backend.addAccount());
    }

    /** @param {string} accountId */
    editAccount(accountId) {
        return this.runGuiOperation(
            t(ACCOUNT_OPERATIONS.edit), () => this.backend.editAccount(accountId));
    }

    /** @param {string} accountId */
    relogin(accountId) {
        return this.runGuiOperation(t(ACCOUNT_OPERATIONS.relogin), async () => {
            const snapshot = await this.backend.relogin(accountId);
            // ログインし直した直後は、前回の失敗が残っていると赤いままになる
            this.cache.delete(accountId);
            return snapshot;
        });
    }

    /**
     * アカウントを1件消します。
     *
     * **runGuiOperation は通しません。** 削除に別ウィンドウは要らないので、
     * バックエンドの中だけで終わります (backend/cli.py の do_delete_account)。
     * ここを GUI 側の道に通していたために、PySide6 が入っていない環境では
     * 削除まで「PySide6 を入れてください」で止まっていました。
     *
     * ログイン画面を開いている最中の削除は、バックエンドが断ります。
     * あちらは開いた時点の一覧を閉じるときに書き戻すので、その間に消しても
     * あとから上書きされて戻ってくるためです。
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
            // 自分より後に始まったものの記録は消しません (runGuiOperation と同じ理由)。
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
