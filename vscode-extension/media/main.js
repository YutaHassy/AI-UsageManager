// @ts-check
/**
 * Webview 側の描画。
 *
 * **DOM は createElement と textContent で組み立てます。** 指標のラベルや
 * 表示文字列は取得先のサーバーが返したものなので、innerHTML に流し込むと
 * 相手の応答がそのまま HTML として動くことになります。
 *
 * **画面は作り直さず、その場で書き換えます。** 状態は1件取得するたびに
 * 届く (待機中 → 取得中 → 完了) ので、6アカウントの更新1回で十数回の
 * 描画が走ります。毎回 DOM を作り直すと、そのたびにスクロール位置が
 * 先頭へ飛び、押しかけのボタンが消え、画面がかくつきます。
 * 作り直すのは「アカウントの顔ぶれが変わったとき」だけです。
 *
 * **しきい値の判定はここにありません。** 色の段階 (level)・丸印 (dot)・
 * 1行要約 (status.summary) はバックエンドが決めたものが届くので、この画面は
 * それを貼るだけです。数値を JavaScript と Python の両方に書くと、片方だけ
 * 直したときに exe と VSCode で違う色に見えます (services/usage_status.py 参照)。
 */
(function () {
    'use strict';

    const vscode = acquireVsCodeApi();

    /**
     * 訳文の辞書。**panel.js が main.js より前に置いてくれます** (panel.js の
     * html() 参照)。webview からは vscode API を触れないので、訳文を持ち込む
     * 経路はここだけです。英語のときは空で、原文がそのまま出ます。
     */
    const BUNDLE = window.__l10n || {};

    /** 表示言語。Intl へ渡すためだけに使います (panel.js が lang 属性に入れます)。 */
    const LANG = document.documentElement.lang || 'en';

    /**
     * 英語の原文を、いまの表示言語に置き換えて返します。
     *
     * 拡張ホスト (i18n.js の t) と同じ規約で、**原文そのものがキー**です。
     * 差し込みは名前付きプレースホルダで書いてください。**文字列を + で
     * 繋がないこと。** 繋いだ時点で、その語順が翻訳者に直せないものとして
     * 固定されます (日本語・韓国語・中国語で並びが変わる箇所が実際にあります)。
     */
    function t(message, args) {
        const text = BUNDLE[message] || message;
        if (!args) { return text; }
        return text.replace(/\{(\w+)\}/g, function (whole, name) {
            return Object.prototype.hasOwnProperty.call(args, name)
                ? String(args[name]) : whole;
        });
    }

    /**
     * 金額の指標だけは、バーの上にパーセントではなく金額を出す。
     * 上限は利用者が決めた任意の値なので、率だけでは元の金額が分からない。
     */
    const MONEY = 'money';

    /** @type {any} */
    let state = {
        accounts: [], entries: {}, fetchable: [], refreshing: false,
        configPath: '', loadError: null, encryptionAvailable: true,
        // 手で決めた並び (accountId) と、いま選ばれている並べ方。**届く前に
        // undefined を触らないよう、ここで形を決めておきます。**
        accountOrder: [], sort: 'manual',
        // 一覧が1度でも届いたか。**「アカウントが0件」と「まだ届いていない」を
        // 区別するために要ります。** 混同すると、開いた直後の一瞬だけ
        // 「アカウントがありません」と出て、登録済みの利用者を驚かせます。
        loaded: false,
    };

    // 「今どちらを見ているか」は画面側の都合なので、ここで持って復元する。
    // null がサマリー (全アカウント)。
    const saved = vscode.getState() || {};
    /** @type {string|null} */
    let selectedId = saved.selectedId || null;

    const el = {
        content: /** @type {HTMLElement} */ (document.getElementById('content')),
        host: /** @type {HTMLElement} */ (document.getElementById('panel-host')),
        refresh: /** @type {HTMLButtonElement} */ (document.getElementById('refresh')),
        progress: /** @type {HTMLElement} */ (document.getElementById('progress')),
        status: /** @type {HTMLElement} */ (document.getElementById('status')),
        tabSummary: /** @type {HTMLButtonElement} */ (document.getElementById('tab-summary')),
        tabDetail: /** @type {HTMLButtonElement} */ (document.getElementById('tab-detail')),
        tabDetailWrap: /** @type {HTMLElement} */ (document.getElementById('tab-detail-wrap')),
        settings: /** @type {HTMLButtonElement} */ (document.getElementById('settings')),
        sort: /** @type {HTMLButtonElement} */ (document.getElementById('sort')),
        language: /** @type {HTMLButtonElement} */ (document.getElementById('language')),
        zoomOut: /** @type {HTMLButtonElement} */ (document.getElementById('zoom-out')),
        zoomLevel: /** @type {HTMLButtonElement} */ (document.getElementById('zoom-level')),
        zoomIn: /** @type {HTMLButtonElement} */ (document.getElementById('zoom-in')),
    };

    // ---------------- 小道具 ----------------

    function make(tag, className, text) {
        const node = document.createElement(tag);
        if (className) { node.className = className; }
        if (text !== undefined && text !== null) { node.textContent = String(text); }
        return node;
    }

    /**
     * 値が変わったときだけ触ります。
     *
     * 同じ文字列を入れ直すだけでも、ブラウザはその要素を描き直します。
     * 1秒ごとのカウントダウンと毎回の状態更新が重なるため、ここを
     * 素通しにすると点滅が見えます。
     */
    function setText(node, text) {
        const value = String(text);
        if (node.textContent !== value) { node.textContent = value; }
    }

    function setClass(node, className) {
        if (node.className !== className) { node.className = className; }
    }

    function setHidden(node, hidden) {
        node.classList.toggle('hidden', Boolean(hidden));
    }

    /**
     * バーの色。
     *
     * **段階を決めるのはバックエンド (Python) です。** level の値
     * (ok / caution / limited) は main.css のクラス名とそのまま同じにして
     * あるので、ここでは繋げるだけです。しきい値の数値をこの画面に
     * 持たせないための形です (services/usage_status.py 参照)。
     */
    function barClass(metric) {
        return 'bar-fill ' + metric.level;
    }

    /**
     * 期間の1区切りを「3日」「3d」「3일」のように整えます。
     *
     * **単位の文字を辞書に持たせません。** Intl が言語ごとの正しい書き方を
     * 知っているので、こちらで「日」「days」を並べると、対応言語を増やすたびに
     * 単位だけを翻訳し直すことになります。narrow を使うのは、この列
     * (.metric-remaining) が固定幅で、英語の "3 days 2 hr" が入らないためです。
     */
    function unitText(value, unit) {
        try {
            return new Intl.NumberFormat(LANG, {
                style: 'unit', unit: unit, unitDisplay: 'narrow',
            }).format(value);
        } catch (e) {
            // ICU が痩せている環境でも、単位が素っ気なくなるだけで済ませる。
            return value + ' ' + unit;
        }
    }

    function remainingText(isoString) {
        if (!isoString) { return ''; }
        const target = new Date(isoString);
        if (isNaN(target.getTime())) { return ''; }
        const diff = target.getTime() - Date.now();
        if (diff <= 0) { return t('Limit lifted'); }

        const total = Math.ceil(diff / 1000);
        const days = Math.floor(total / 86400);
        const hours = Math.floor((total % 86400) / 3600);
        const minutes = Math.floor((total % 3600) / 60);
        const seconds = total % 60;

        // 1分を切ったら秒で出す。分だけにすると「0分後」で止まって見えます。
        if (days === 0 && hours === 0 && minutes === 0) {
            return t('in {duration}', { duration: unitText(seconds, 'second') });
        }
        const parts = [];
        if (days > 0) { parts.push(unitText(days, 'day')); }
        if (hours > 0 || days > 0) { parts.push(unitText(hours, 'hour')); }
        parts.push(unitText(minutes, 'minute'));
        return t('in {duration}', { duration: parts.join(' ') });
    }

    function resetText(isoString) {
        if (!isoString) { return ''; }
        const target = new Date(isoString);
        if (isNaN(target.getTime())) { return ''; }
        let when;
        try {
            // 年月日の並びは言語ごとに違う (2026/08/08 と 8/8/2026)。
            // 決め打ちにせず Intl に任せます。
            when = new Intl.DateTimeFormat(LANG, {
                dateStyle: 'short', timeStyle: 'medium',
            }).format(target);
        } catch (e) {
            when = target.toLocaleString();
        }
        return t('Resets: {when}', { when: when });
    }

    /** 残り時間の表示。1秒ごとの更新対象にするかどうかもここで決める。 */
    function setResets(node, isoString) {
        if (isoString) {
            node.dataset.resetsAt = isoString;
            setText(node, remainingText(isoString));
        } else {
            delete node.dataset.resetsAt;
            setText(node, '');
        }
    }

    /** ゲージに出せる指標だけを、取得先が返した順に返す。 */
    function gaugedMetrics(usage) {
        const metrics = (usage && usage.metrics) || [];
        return metrics.filter((m) => m.utilization !== null && m.utilization !== undefined);
    }

    function entryOf(account) {
        return state.entries[account.id] || { state: 'unknown' };
    }

    /**
     * アカウント1件の状態。デスクトップ版の account_state と同じ順で判定する
     * (場所によって違う色・違う文言に見えないようにするため)。
     */
    function accountState(account) {
        const entry = entryOf(account);
        if (!account.enabled) { return { dot: '⚪', note: t('Disabled'), utilization: null }; }
        if (!account.implemented) { return { dot: '⚫', note: t('Not ready'), utilization: null }; }
        if (entry.state === 'loading') { return { dot: '🔵', note: t('Fetching...'), utilization: null }; }
        if (entry.state === 'queued') { return { dot: '🔵', note: t('Waiting...'), utilization: null }; }
        if (entry.state === 'error') {
            return {
                dot: '🔴',
                note: entry.authError ? t('Sign-in needed') : t('Error'),
                utilization: null,
            };
        }
        // 判定済みの状態 (status) が載っていない結果は未取得と同じ扱いにする。
        // 丸印や要約をこちらで作り直すと、しきい値がまたこの画面に戻る。
        if (entry.state !== 'ok' || !(entry.usage && entry.usage.status)) {
            if (account.needsCredential && !account.hasCredential) {
                return {
                    dot: '⚫',
                    note: t('{credential} not set', { credential: account.credentialLabel }),
                    utilization: null,
                };
            }
            return { dot: '⚫', note: t('Not fetched'), utilization: null };
        }
        // ここから先はバックエンドが決めたものをそのまま出す。
        const status = entry.usage.status;
        return {
            dot: status.dot,
            note: status.summary,
            utilization: entry.usage.max_utilization,
        };
    }

    // ---------------- 並び順 ----------------
    //
    // **state.accounts の並びは「追加した順」です。** バックエンドは並べ替え
    // ません (backend/cli.py の snapshot)。あちらの格納順そのものが「追加した
    // 順」の唯一の記録なので、書き換えるとその基準に戻せなくなります。手で
    // 決めた並びは accountOrder として別に届くので、ここで突き合わせます。

    /**
     * 保存の応答を待つあいだ、先に描いておく並び。**待たないのは、応答まで
     * 行が動かないと「掴んで落としたのに戻った」ように見えるため**です。
     * 保存を頼んでいないあいだは null で、そのときは届いた並びをそのまま使います。
     *
     * @type {string[]|null}
     */
    let localOrder = null;

    /** localOrder を描いているあいだの並べ方 (手で動かした時点で 'manual')。 @type {string|null} */
    let localSort = null;

    /** 保存が失敗したと確定したか。**推測では立てません** (拡張ホストが伝えてきます)。 */
    let orderSaveFailed = false;

    /** 同じ id が同じ順で並んでいるか。 */
    function sameIds(a, b) {
        const left = a || [];
        const right = b || [];
        if (left.length !== right.length) { return false; }
        for (let i = 0; i < left.length; i++) {
            if (left[i] !== right[i]) { return false; }
        }
        return true;
    }

    /**
     * 楽観的な上書きを、**もう要らなくなったときだけ**捨てます。
     *
     * 捨ててよいのは「送った並びがそのまま返ってきた」ときと「保存できな
     * かったと確定した」ときの2つだけです。**「新しい状態が届いたら捨てる」
     * にしてはいけません** — 状態は1回の更新で十数回飛ぶので (panel.js の
     * post)、保存の応答より先に届いたぶんで捨てると、古い並びで描き直して
     * から新しい並びへ戻る、という一往復の点滅になります。
     */
    function releaseLocalOrder(message) {
        if (localOrder === null) { return; }
        if (sameIds(message.accountOrder, localOrder) || orderSaveFailed) {
            localOrder = null;
            localSort = null;
            orderSaveFailed = false;
            clearOrderTimeout();
        }
    }

    /**
     * 使用率順に並べるときの率。**行に出ている数字と同じものを使います。**
     *
     * accountState の utilization ではありません。あちらは取得中・待機中に
     * 無条件で null を返します (「取得中...」と出すために必要な null です)。
     * それで並べると、全件更新が走った瞬間に全員が null へ落ちて一覧が丸ごと
     * 名前順に崩れ、結果が1件届くたびに行が飛びます。既定の更新間隔は1分な
     * ので (package.json の autoRefreshMinutes)、これは例外ではなく毎分起きる
     * 普通の動作です。
     *
     * 行のほうは同じ理由で前回の値を出し続けています (updateSummaryRow の
     * 「取得中も前回の値を出し続けます」)。順序もそちらへ合わせます — 見えて
     * いる数字と並びが食い違うほうが分かりません。取得できていないものは
     * store が usage を持たないので (store.js の setEntry。エラーのときは
     * 前回の値ごと捨てます) null になり、末尾へ回ります。
     */
    function sortUtilization(account) {
        const usage = entryOf(account).usage;
        const value = usage ? usage.max_utilization : null;
        return typeof value === 'number' && isFinite(value) ? value : null;
    }

    /** いま効いている並べ方。 */
    function currentSort() {
        return localSort || state.sort || 'manual';
    }

    /** 名前で並べる。同順位の落ち着き先としても使います。 */
    function compareByName(a, b) {
        return String(a.name || '').localeCompare(String(b.name || ''), LANG);
    }

    /**
     * 画面に出す順に並べたアカウントを返します。
     *
     * **state.accounts は並べ替えません。** 破壊的に並べ替えると「追加した順」
     * が失われるので、いつも新しい配列を作って返します。
     *
     * **しきい値は作りません。** 使用率順に使うのはバックエンドが入れた
     * max_utilization だけです (sortUtilization 参照)。数値が無いもの
     * (取得前・エラー) は末尾へ回します。0% と「取れていない」を同じ場所に
     * 置くと、区別が付かなくなります。
     */
    function orderedAccounts() {
        const accounts = state.accounts || [];
        const sort = currentSort();

        if (sort === 'manual') {
            return manualOrder(accounts, localOrder || state.accountOrder || []);
        }
        if (sort === 'added') {
            return accounts.slice();
        }
        if (sort === 'name') {
            return accounts.slice().sort(compareByName);
        }
        if (sort === 'provider') {
            return accounts.slice().sort((a, b) => {
                const byProvider = String(a.providerLabel || '')
                    .localeCompare(String(b.providerLabel || ''), LANG);
                return byProvider !== 0 ? byProvider : compareByName(a, b);
            });
        }
        if (sort === 'usage') {
            // 並べ替えの比較は何度も呼ばれるので、率は先に1回だけ引きます。
            const rates = new Map();
            for (const account of accounts) {
                rates.set(account.id, sortUtilization(account));
            }
            return accounts.slice().sort((a, b) => {
                const left = rates.get(a.id);
                const right = rates.get(b.id);
                if (left === null && right === null) { return compareByName(a, b); }
                if (left === null) { return 1; }
                if (right === null) { return -1; }
                if (left !== right) { return right - left; }
                return compareByName(a, b);
            });
        }
        // 知らない基準 (設定ファイルを手で書き換えた等) は、並べないでおきます。
        return accounts.slice();
    }

    /**
     * 手で決めた並びと、いま居るアカウントを突き合わせます。
     *
     * **並びのほうが古くても構いません。** 消えたアカウントの id は無視し、
     * 並びに無いアカウント (新しく足したもの) は末尾へ付けます。末尾なのは、
     * 追加が末尾に積まれる (cli.py の create_account) のと揃えるためです。
     * **ここで保存し直しはしません** — 毎回突き合わせるので実害がなく、
     * 表示のたびに設定ファイルを書き換えるほうがよほど乱暴です。
     */
    function manualOrder(accounts, order) {
        const byId = new Map();
        for (const account of accounts) { byId.set(account.id, account); }

        const result = [];
        const placed = new Set();
        for (const id of order) {
            const account = byId.get(id);
            if (account && !placed.has(id)) {
                result.push(account);
                placed.add(id);
            }
        }
        for (const account of accounts) {
            if (!placed.has(account.id)) { result.push(account); }
        }
        return result;
    }

    /**
     * 並べ方のラベル。**extension.js の選択肢と同じ原文を使います** —
     * 選んだものと出ているものが違う言い回しだと、選んだ結果が確かめられません。
     */
    function sortLabel(sort) {
        if (sort === 'usage') { return t('Highest usage first'); }
        if (sort === 'name') { return t('By name'); }
        if (sort === 'provider') { return t('By provider'); }
        if (sort === 'added') { return t('In the order they were added'); }
        return t('Keep the order you arranged by hand');
    }

    // ---------------- 部品 ----------------

    /**
     * 1つの枠 (5時間 / 週間 / 当月コスト …)。
     *
     * 枠名と残り時間は固定幅、バーだけが伸びます。そうすることで、
     * アカウントをまたいでも枠をまたいでもバーの左右が揃い、長さを
     * そのまま比べられます。
     *
     * **バーと、バーを出せないときの文言は同じ場所に置きます。** 別々の列に
     * すると、片方を隠したときにもう片方が動いてバーがずれます。
     */
    function makeMetricLine() {
        const root = make('div', 'metric-line');
        const label = make('span', 'metric-label', '');
        root.appendChild(label);

        const cell = make('div', 'metric-cell');
        const bar = make('div', 'bar');
        const fill = make('div', 'bar-fill');
        const barText = make('span', 'bar-text', '');
        bar.appendChild(fill);
        bar.appendChild(barText);
        const note = make('span', 'metric-note', '');
        cell.appendChild(bar);
        cell.appendChild(note);
        root.appendChild(cell);

        const remaining = make('span', 'metric-remaining', '');
        root.appendChild(remaining);

        return { root, label, bar, fill, barText, note, remaining };
    }

    /** metric が null のときは、バーの代わりに fallbackNote を出します。 */
    function updateMetricLine(line, metric, fallbackNote) {
        if (!metric) {
            setText(line.label, '');
            setHidden(line.bar, true);
            setHidden(line.note, false);
            setText(line.note, fallbackNote || '');
            setResets(line.remaining, null);
            return;
        }

        setText(line.label, metric.label || '');
        // 枠名の列は固定幅なので、長い名前は省略記号で切れます (main.css)。
        // 切れた分をツールチップで読めるようにしておきます。日本語では
        // まず切れませんが、英語・韓国語では実際に切れます。
        line.label.title = metric.label || '';
        const utilization = metric.utilization;
        if (utilization === null || utilization === undefined) {
            setHidden(line.bar, true);
            setHidden(line.note, false);
            setText(line.note, metric.display || '-');
        } else {
            const clamped = Math.max(0, Math.min(100, utilization));
            setHidden(line.bar, false);
            setHidden(line.note, true);
            const width = clamped + '%';
            if (line.fill.style.width !== width) { line.fill.style.width = width; }
            setClass(line.fill, barClass(metric));
            // 金額はパーセントではなく金額を出す
            setText(line.barText,
                metric.kind === MONEY ? (metric.display || '') : clamped.toFixed(1) + '%');
        }
        setResets(line.remaining, metric.resets_at);
    }

    /**
     * 必要な本数だけ行を用意します (多い分は消します)。
     *
     * 毎回作り直さないのは、1秒ごとのカウントダウンや取得のたびに
     * 作り替えが走ると画面がちらつくためです。
     */
    function ensureChildren(container, list, count, factory) {
        while (list.length < count) {
            const item = factory();
            container.appendChild(item.root);
            list.push(item);
        }
        while (list.length > count) {
            const item = list.pop();
            item.root.remove();
        }
        return list;
    }

    /** 操作ボタン。 */
    function makeButton(className, onClick) {
        const button = /** @type {HTMLButtonElement} */ (make('button', className, ''));
        button.type = 'button';
        button.addEventListener('click', onClick);
        return button;
    }

    function updateButton(button, label, tooltip) {
        setText(button, label);
        button.title = tooltip;
    }

    // ---------------- サマリー ----------------

    function makeSummaryRow(accountId) {
        const root = make('div', 'summary-row');
        root.tabIndex = 0;
        root.setAttribute('role', 'button');
        // **落とす先を決めるとき、DOM の並びから id を読みます。** 掴んで
        // いるあいだは描き直しを止めるので (dragging)、そのとき正しいのは
        // 計算し直した並びではなく、いま画面に出ている並びのほうです。
        root.dataset.accountId = accountId;

        const header = make('div', 'summary-header');
        const dot = make('span', 'dot', '');
        const name = make('span', 'summary-name', '');
        header.appendChild(dot);
        header.appendChild(name);
        // つまみは .summary-header の末尾に置きます (main.css の .drag-handle)。
        const handle = make('span', 'drag-handle', '≡');
        // **role="button" は付けません。** 焦点を当てられないものを「ボタン」
        // と伝えると、支援技術には起動できるものとして見えるのに、キーボード
        // からは押せません。名前 (aria-label) は updateSummaryRow が入れます。
        // 掴まずに動かす手立ては行の Alt+↑↓ で用意してあります。

        // **焦点は行 (root) が持ちます。** つまみにも焦点を持たせると、Tab の
        // 回数がアカウント数の2倍になり、キーボードだけで一覧を通り抜けるのが
        // 倍かかります。Alt+↑↓ は行に付けてあるので、これで足ります。
        handle.tabIndex = -1;
        // **掴んだだけで詳細が開かないようにします。** click は行にも付いて
        // いるので、止めないと落とした直後にその行が開きます。
        handle.addEventListener('click', (e) => e.stopPropagation());
        handle.addEventListener('pointerdown', (e) => beginDrag(e, accountId, root, handle));
        handle.addEventListener('pointermove', onDragMove);
        // **掴んでいるポインタのものだけを見ます** (onDragMove と同じ照合)。
        // 照合しないと、2本目の指が別の行のつまみに触れて離しただけで、1本目が
        // まだ触れているのに、その時点の落とし先で確定して保存が飛びます。
        handle.addEventListener('pointerup', (e) => {
            if (isDragPointer(e)) { endDrag(true); }
        });
        // **取り消しでも必ず後始末します。** 落として終わるとは限りません。
        handle.addEventListener('pointercancel', (e) => {
            if (isDragPointer(e)) { endDrag(false); }
        });
        header.appendChild(handle);
        root.appendChild(header);

        const lines = make('div', 'summary-lines');
        root.appendChild(lines);

        const open = () => { selectedId = accountId; persist(); render(); };
        root.addEventListener('click', open);
        root.addEventListener('keydown', (e) => {
            // **Alt で切り分けます。** こうしておけば、素の Enter / Space も、
            // 素の ↑ / ↓ (焦点の移動はブラウザ任せ) も今までどおりです。
            // VSCode 本体の Alt+↑↓ (行の移動) は when: editorTextFocus が
            // 付いているので、webview に焦点があるあいだは発火しません。
            if (e.altKey && (e.key === 'ArrowUp' || e.key === 'ArrowDown')) {
                e.preventDefault();
                moveAccount(accountId, e.key === 'ArrowUp' ? -1 : 1);
                return;
            }
            if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); }
        });

        return { root, dot, name, handle, lines, lineNodes: [] };
    }

    function updateSummaryRow(row, account) {
        const info = accountState(account);
        setText(row.dot, info.dot);
        setText(row.name, t('{name} [{provider}]',
            { name: account.name, provider: account.providerLabel }));
        // **記号 (≡) だけでは、読み上げても何のつまみか分かりません。**
        // 名前と結び付けておかないと、一覧の中で同じ読み上げが人数ぶん並びます。
        const moveLabel = t('Move {name}', { name: account.name });
        if (row.handle.getAttribute('aria-label') !== moveLabel) {
            row.handle.setAttribute('aria-label', moveLabel);
        }

        const entry = entryOf(account);
        // **取得中も前回の値を出し続けます。** 「取得中...」の1行に差し替えると
        // 枠の数だけ行が減り、カードの高さが変わって表全体が上下にずれます。
        // 取得中であることは丸印とツールバーの「更新中...」で分かります。
        const gauged = entry.usage ? gaugedMetrics(entry.usage) : [];
        // 枠が複数あるなら全部出す。最も逼迫しているものだけを出すと、
        // 5時間枠が空いていることは分かっても、週間枠を使い切って
        // いることに気づけない。
        ensureChildren(row.lines, row.lineNodes, Math.max(1, gauged.length), makeMetricLine);
        if (gauged.length > 0) {
            gauged.forEach((metric, index) => updateMetricLine(row.lineNodes[index], metric));
        } else {
            updateMetricLine(row.lineNodes[0], null, info.note);
        }
    }

    function makeSummary() {
        const root = make('div', 'panel');
        root.appendChild(make('h2', 'panel-title', t('All accounts')));
        const list = make('div', 'summary-list');
        root.appendChild(list);
        const empty = make('p', 'hint', '');
        root.appendChild(empty);
        const hint = make('p', 'hint', t('Click a row to open that account.'));
        root.appendChild(hint);
        // **手順を書いておきます。** つまみは見れば掴めそうだと分かりますが、
        // キーボードでも動かせることは、触っても見ても分かりません。
        const orderHint = make('p', 'hint',
            t('Drag to reorder. Alt+Up and Alt+Down move the selected row.'));
        root.appendChild(orderHint);

        const actions = make('div', 'actions');
        const addButton = makeButton('ghost', () => vscode.postMessage({ type: 'addAccount' }));
        actions.appendChild(addButton);
        root.appendChild(actions);

        return { root, list, empty, hint, orderHint, actions, addButton, rows: new Map() };
    }

    function updateSummary(view) {
        // **並べるのはここだけです。** 行は accountId で使い回し、位置が
        // 違うときだけ動かすので (下の insertBefore)、並びが変わっても
        // 作り直しは起きません。
        const accounts = orderedAccounts();

        // 顔ぶれが変わったときだけ行を作り直す。並びが同じなら触らない。
        const alive = new Set();
        accounts.forEach((account, index) => {
            alive.add(account.id);
            let row = view.rows.get(account.id);
            if (!row) {
                row = makeSummaryRow(account.id);
                view.rows.set(account.id, row);
            }
            if (view.list.children[index] !== row.root) {
                view.list.insertBefore(row.root, view.list.children[index] || null);
            }
            updateSummaryRow(row, account);
        });
        for (const [id, row] of Array.from(view.rows)) {
            if (!alive.has(id)) {
                row.root.remove();
                view.rows.delete(id);
            }
        }

        const hasAccounts = accounts.length > 0;
        setHidden(view.list, !hasAccounts);
        setHidden(view.hint, !hasAccounts);
        setHidden(view.orderHint, !hasAccounts);
        setHidden(view.empty, hasAccounts);
        setText(view.empty, state.loaded
            ? t('No accounts yet. Use "＋ Add" to register one.')
            : t('Loading...'));
        setHidden(view.actions, !state.loaded);
        updateButton(view.addButton, t('＋ Add'),
            t('Opens the form for registering an account'));
    }

    // ---------------- 並べ替えの操作 ----------------
    //
    // **落とす位置は、矩形とポインタの相対比較だけで決めます。** 倍率 (zoom)
    // を計算に持ち込みません。getBoundingClientRect() の返り値と
    // PointerEvent.clientY は同じ座標空間にいるので、「ポインタが行の上半分に
    // いるか下半分にいるか」を見るだけなら、倍率がいくつであるかを知る必要が
    // ありません。倍率を掛ける式を1つでも書くと、その式は倍率の当て方
    // (body の zoom) に縛られ、当て方を替えた日に静かにずれ始めます。

    /**
     * 掴んでいるあいだの状態。**null でないあいだは描き直しを止めます**
     * (下の renderPending)。
     *
     * @type {{pointerId: number, accountId: string, root: HTMLElement,
     *         handle: HTMLElement, targetId: string|null, before: boolean}|null}
     */
    let dragging = null;

    /**
     * 次に来る click を1つだけ捨てるか。
     *
     * **掴みを取り消したあとにマウスを離すと、そのままでは行が開きます。**
     * 捕まえ (setPointerCapture) を外した時点から、pointerup の届き先は
     * つまみではなく行の本体に変わります。click が起きる場所は pointerdown と
     * pointerup の共通の親なので、両方が同じ行の中にあると**行そのもの**に
     * なり、つまみに付けた stopPropagation はもう通り道にいません。Esc でも、
     * ウィンドウの非アクティブ化でも、タブを隠したときでも同じです。
     * **取り消したのに詳細が開くのでは、取り消しになっていません。**
     */
    let swallowNextClick = false;

    /**
     * 掴んでいるあいだに届いた状態があるか。
     *
     * **状態そのものは捨てません。止めるのは描き直しだけです。** 捨てると
     * 取得結果を1回分落とします。1回の全件更新で状態は十数回飛ぶので
     * (panel.js の post)、掴んだまま描き直すと、枠の本数が変わって行の高さが
     * 変わり (updateSummaryRow の ensureChildren)、狙っていた落とし先が指の
     * 下から逃げます。1秒ほど数字が止まって見えるほうが実害は小さいのです。
     */
    let renderPending = false;

    /** 最後に見たポインタの縦位置。自動スクロール中の計算し直しに使います。 */
    let lastPointerY = 0;

    /** @type {number|null} 自動スクロールの予約 (requestAnimationFrame)。 */
    let autoScrollFrame = null;

    /** 1フレームで送る量。0 なら止まっています。 */
    let autoScrollStep = 0;

    /** 縁とみなす帯の幅。**clientY と同じ空間の値です。** */
    const EDGE_BAND = 28;

    /**
     * 1フレームあたり .content を送る量。
     *
     * **scrollTop は zoom の外側の単位です** (波2 が実測しました)。倍率を
     * 上げると同じ値でも画面上は速く見えますが、ここで倍率を掛けて見た目を
     * 揃えることはしません。ポインタ座標と scrollTop を混ぜた式を書かない、
     * というのがこの機能全体の約束だからです。速さの違いは、目的地へ運べなく
     * なるほどのものではありません。
     */
    const AUTO_SCROLL_STEP = 8;

    /** @type {number|null} 応答も失敗の知らせも来ないまま固まったときの保険。 */
    let orderTimeout = null;

    /**
     * いま画面に並んでいる順の accountId。**見えているものが正です。**
     *
     * 掴んでいるあいだは描き直しを止めているので、orderedAccounts() を呼び
     * 直すと、画面に出ていない並び (その間に届いた状態で計算した並び) が返る
     * ことがあります。落とした結果は、利用者が見ていた並びに差し込んだもので
     * なければなりません。
     */
    function visibleIds() {
        const view = views.summary;
        if (!view || views.mode !== 'summary') {
            return orderedAccounts().map((account) => account.id);
        }
        const ids = [];
        for (const node of Array.from(view.list.children)) {
            const id = /** @type {HTMLElement} */ (node).dataset.accountId;
            if (id) { ids.push(id); }
        }
        return ids;
    }

    /**
     * 1件を動かした並びを新しく作って返します。**元の配列は触りません。**
     *
     * @param {string[]} ids いまの並び
     * @param {string} movedId 動かすもの
     * @param {string|null} targetId 目印にする行 (null なら末尾へ)
     * @param {boolean} before 目印の手前へ入れるか
     * @returns {string[]}
     */
    function orderWithMove(ids, movedId, targetId, before) {
        // **自分の上に落としたときは動きません。** ここを素通しにすると、
        // 目印を除いた並びから自分自身を探すことになり、「見つからない」の
        // 行き先 (末尾) へ飛びます。掴んだ場所にそっと戻したら末尾へ回された、
        // というのがいちばん納得のいかない壊れ方です。
        if (targetId === movedId) { return ids.slice(); }
        const rest = ids.filter((id) => id !== movedId);
        const at = targetId === null ? -1 : rest.indexOf(targetId);
        if (at < 0) {
            rest.push(movedId);
            return rest;
        }
        rest.splice(before ? at : at + 1, 0, movedId);
        return rest;
    }

    /**
     * 並びを確定します。**ドラッグからもキーボードからもここへ来ます。**
     * 経路を2本にすると、片方だけ楽観的な反映や手動への切り替えを忘れます。
     *
     * 保存の応答を待たずに先に描くのは、待つと「掴んで落としたのに戻った」
     * ように見えるためです。先に描いたものを捨てる条件は releaseLocalOrder が
     * 持っています。**ここでは捨てません。**
     *
     * @param {string[]} ids
     */
    function commitOrder(ids) {
        // **いま居るアカウントの id だけを送ります。** 並びは画面に出ている
        // 行から読むので (visibleIds)、掴んでいるあいだにアカウントが消えると
        // 消えた id が混ざります。バックエンドは知らない id が1つでもあると
        // 全体を弾くので (backend/cli.py の reorder_accounts)、利用者は何も
        // 間違えていないのに操作が捨てられ、「タブを開き直せ」という不要な
        // 指示まで受けます。
        const known = new Set((state.accounts || []).map((account) => account.id));
        ids = ids.filter((id) => known.has(id));
        // **1件も残らなかったら送りません。** 空の並びは「全部消す」として
        // 保存されるので (backend/cli.py の reorder_accounts)、掴んでいる間に
        // 全部消えたというだけで、手で決めた並びが黙って失われます。
        if (ids.length === 0) { return; }
        localOrder = ids;
        // **基準で並べている最中に動かしたら、手動へ切り替えます。** 基準の
        // ままにすると、次の自動更新で並びが基準へ戻り、動かした操作が
        // なかったことになります。設定そのものを書き換えるのは拡張ホスト側です
        // (panel.js の case 'reorderAccounts')。ここはそれが届くまでの繋ぎです。
        localSort = 'manual';
        orderSaveFailed = false;
        armOrderTimeout();
        render();
        vscode.postMessage({ type: 'reorderAccounts', order: ids });
    }

    /**
     * 先に描いたものに時限を付けます。
     *
     * 保存の応答も失敗の知らせも来ないまま (バックエンドが固まった等)、画面が
     * 新しい並びに張り付き続けるのを防ぎます。**普通は使われません** — 応答が
     * 返れば releaseLocalOrder のほうが先に捨てます。
     */
    function armOrderTimeout() {
        clearOrderTimeout();
        orderTimeout = setTimeout(() => {
            orderTimeout = null;
            if (localOrder === null) { return; }
            localOrder = null;
            localSort = null;
            if (dragging) { renderPending = true; } else { render(); }
        }, 10000);
    }

    function clearOrderTimeout() {
        if (orderTimeout !== null) {
            clearTimeout(orderTimeout);
            orderTimeout = null;
        }
    }

    /**
     * キーボードで1つ動かします (Alt+↑ / Alt+↓)。
     *
     * @param {string} accountId
     * @param {number} delta -1 で上へ、+1 で下へ
     */
    function moveAccount(accountId, delta) {
        const ids = visibleIds();
        const at = ids.indexOf(accountId);
        const to = at + delta;
        if (at < 0 || to < 0 || to >= ids.length) { return; }
        const next = ids.slice();
        next.splice(at, 1);
        next.splice(to, 0, accountId);
        commitOrder(next);
        // **焦点を行に残します。** 並べ替えは行を insertBefore で動かすので、
        // 何もしないと焦点が body へ落ち、2回目の Alt+↓ が効きません。
        const view = views.summary;
        const row = view ? view.rows.get(accountId) : null;
        if (row) { row.root.focus(); }
    }

    /**
     * 掴み始め。**行全体ではなく、つまみからしか始まりません。**
     *
     * 行には「開く」の click が付いているので、行全体を掴めるようにすると、
     * 同じ pointerdown から始まる2つの操作がぶつかり、「開こうとしたら並べ
     * 替わる」「並べ替えようとしたら開く」のどちらかが起きます。
     *
     * @param {PointerEvent} event
     * @param {string} accountId
     * @param {HTMLElement} root
     * @param {HTMLElement} handle
     */
    function beginDrag(event, accountId, root, handle) {
        // 主ボタン以外 (右クリック等) では始めません。
        if (dragging || event.button !== 0) { return; }
        const view = views.summary;
        if (!view) { return; }
        // **選択の開始と、既定のドラッグを止めます。** setPointerCapture では
        // どちらも止まりません。
        event.preventDefault();
        event.stopPropagation();
        dragging = {
            pointerId: event.pointerId, accountId: accountId,
            root: root, handle: handle, targetId: null, before: true,
        };
        // **捕まえておきます。** 掴んだ指が行の外や画面の端へ出ても
        // pointermove が届き続けないと、途中で印が止まります。
        try {
            handle.setPointerCapture(event.pointerId);
        } catch (e) {
            // 既に離されている等。捕まえられなくても掴み自体は続けられます。
        }
        view.list.classList.add('is-dragging');
        root.classList.add('is-dragging');
        lastPointerY = event.clientY;
        updateDropTarget(event.clientY);
    }

    /** 掴んでいるポインタからの知らせか。 @param {PointerEvent} event */
    function isDragPointer(event) {
        return dragging !== null && event.pointerId === dragging.pointerId;
    }

    /** @param {PointerEvent} event */
    function onDragMove(event) {
        if (!dragging || event.pointerId !== dragging.pointerId) { return; }
        event.preventDefault();
        lastPointerY = event.clientY;
        updateDropTarget(event.clientY);
        updateAutoScroll(event.clientY);
    }

    /**
     * 落とす先を決めて、印を付け替えます。
     *
     * **上から順に見て、ポインタより中心が下にある最初の行**が挿入位置です。
     * どれも当てはまらなければ末尾。使うのは矩形とポインタの相対比較だけで、
     * 倍率も、スクロール量も、絶対座標も出てきません。
     *
     * @param {number} clientY
     */
    function updateDropTarget(clientY) {
        if (!dragging) { return; }
        const view = views.summary;
        if (!view) { return; }
        const rows = Array.from(view.list.children);
        /** @type {string|null} */
        let targetId = null;
        let before = true;
        for (const node of rows) {
            const row = /** @type {HTMLElement} */ (node);
            const rect = row.getBoundingClientRect();
            if (clientY < rect.top + rect.height / 2) {
                targetId = row.dataset.accountId || null;
                before = true;
                break;
            }
        }
        if (targetId === null && rows.length > 0) {
            const last = /** @type {HTMLElement} */ (rows[rows.length - 1]);
            targetId = last.dataset.accountId || null;
            before = false;
        }
        if (targetId === dragging.targetId && before === dragging.before) { return; }
        dragging.targetId = targetId;
        dragging.before = before;
        renderDropMarker();
    }

    /**
     * 落とす先の印。**枠ではなく片側の線だけ**を付けます (main.css)。
     * border にすると行の高さが変わり、掴んでいるあいだ下の行がずれ続けます。
     */
    function renderDropMarker() {
        const view = views.summary;
        if (!view) { return; }
        const targetId = dragging ? dragging.targetId : null;
        const before = dragging ? dragging.before : true;
        for (const node of Array.from(view.list.children)) {
            const row = /** @type {HTMLElement} */ (node);
            const hit = targetId !== null && row.dataset.accountId === targetId;
            row.classList.toggle('drop-before', hit && before);
            row.classList.toggle('drop-after', hit && !before);
        }
    }

    /**
     * 縁に近づいているあいだ、一覧を送ります。
     *
     * **これが無いと、画面外の位置へは落とせません。** この画面は既定で
     * エディタの隣 (半分幅) に開き、そのうえ 200% まで拡大できるので、一覧が
     * .content に収まらないほうが普通です。掴めるつまみが出ているのに目的地へ
     * 運べないのは、掴めないことより悪い見え方です。
     *
     * @param {number} clientY
     */
    function updateAutoScroll(clientY) {
        const box = el.content.getBoundingClientRect();
        if (clientY < box.top + EDGE_BAND) {
            autoScrollStep = -AUTO_SCROLL_STEP;
        } else if (clientY > box.bottom - EDGE_BAND) {
            autoScrollStep = AUTO_SCROLL_STEP;
        } else {
            autoScrollStep = 0;
        }
        if (autoScrollStep !== 0 && autoScrollFrame === null) {
            autoScrollFrame = requestAnimationFrame(stepAutoScroll);
        }
    }

    function stepAutoScroll() {
        autoScrollFrame = null;
        if (!dragging || autoScrollStep === 0) { return; }
        const before = el.content.scrollTop;
        el.content.scrollTop = before + autoScrollStep;
        // **送ったら落とす先を計算し直します。** 指が止まっていても行のほうが
        // 動くので、印だけが取り残されます。
        updateDropTarget(lastPointerY);
        // 端に着いたら止めます (押し付けたまま回し続けても意味がありません)。
        if (el.content.scrollTop === before) { return; }
        autoScrollFrame = requestAnimationFrame(stepAutoScroll);
    }

    function stopAutoScroll() {
        autoScrollStep = 0;
        if (autoScrollFrame !== null) {
            cancelAnimationFrame(autoScrollFrame);
            autoScrollFrame = null;
        }
    }

    /**
     * 掴むのをやめます。**どんな終わり方でも必ずここを通します。**
     *
     * 落として終わるとは限りません (Esc、ウィンドウの非アクティブ化、タブを
     * 隠す、ポインタの取り消し)。このパネルは隠しても生きたままなので
     * (panel.js の retainContextWhenHidden)、後始末を忘れると、見えない画面の
     * 中で掴んだままになり、次に開いたとき描き直しが止まったままになります。
     *
     * @param {boolean} commit 落とした位置で確定するか
     */
    function endDrag(commit) {
        const drag = dragging;
        if (!drag) { return; }
        dragging = null;
        // **離したときの click を1つ捨てます** (swallowNextClick の説明を参照)。
        // 落として終わったときも立てておきます — そちらは click がつまみに
        // 当たって止まるので、余ったぶんは次に押したときに下ろされます。
        swallowNextClick = true;
        stopAutoScroll();
        try {
            drag.handle.releasePointerCapture(drag.pointerId);
        } catch (e) {
            // 既に離れている。捕まえていないものを離しても困りません。
        }
        const view = views.summary;
        if (view) { view.list.classList.remove('is-dragging'); }
        drag.root.classList.remove('is-dragging');
        renderDropMarker();

        const ids = visibleIds();
        const next = commit && drag.targetId !== null
            ? orderWithMove(ids, drag.accountId, drag.targetId, drag.before)
            : null;
        if (next && !sameIds(next, ids)) {
            // commitOrder が render() まで面倒を見ます。掴んでいるあいだに
            // 溜めた状態も、そこで一緒に描かれます。
            renderPending = false;
            commitOrder(next);
            return;
        }
        // 動かなかったときも、止めていた描き直しに追いつきます。
        if (renderPending) {
            renderPending = false;
            render();
        }
    }
    // ---------------- 詳細 ----------------

    function makeMetricBlock() {
        const root = make('div', 'metric-block');
        const title = make('div', 'metric-title', '');
        root.appendChild(title);

        const value = make('div', 'metric-value', '');
        root.appendChild(value);

        const bar = make('div', 'bar bar-large');
        const fill = make('div', 'bar-fill');
        const barText = make('span', 'bar-text', '');
        bar.appendChild(fill);
        bar.appendChild(barText);
        root.appendChild(bar);

        const times = make('div', 'metric-times');
        const reset = make('span', 'metric-reset', '');
        const remaining = make('span', 'metric-remaining', '');
        times.appendChild(reset);
        times.appendChild(remaining);
        root.appendChild(times);

        return { root, title, value, bar, fill, barText, times, reset, remaining };
    }

    function updateMetricBlock(block, metric) {
        setText(block.title, metric.kind === 'percent'
            ? t('{label} utilization', { label: metric.label || '' })
            : (metric.label || ''));

        const utilization = metric.utilization;
        if (utilization === null || utilization === undefined) {
            setHidden(block.bar, true);
            setHidden(block.value, false);
            setText(block.value, metric.display || '-');
        } else {
            const clamped = Math.max(0, Math.min(100, utilization));
            setHidden(block.bar, false);
            setHidden(block.value, true);
            const width = clamped + '%';
            if (block.fill.style.width !== width) { block.fill.style.width = width; }
            setClass(block.fill, barClass(metric));
            setText(block.barText, metric.display || clamped.toFixed(1) + '%');
        }

        // リセットの概念が無い指標 (課金額など) では時刻行を出さない
        setHidden(block.times, !metric.resets_at);
        setText(block.reset, resetText(metric.resets_at));
        setResets(block.remaining, metric.resets_at);
    }

    function makeDetail() {
        const root = make('div', 'panel');
        const name = make('h2', 'account-name', '');
        const source = make('p', 'account-source', '');
        root.appendChild(name);
        root.appendChild(source);
        root.appendChild(make('hr', 'sep'));

        const blocks = make('div', 'metric-blocks');
        root.appendChild(blocks);

        const hint = make('p', 'hint', '');
        root.appendChild(hint);

        const errorBox = make('div', 'error-box');
        const errorTitle = make('div', 'error-title', '');
        const errorBody = make('div', 'error-body', '');
        const errorHint = make('div', 'hint', '');
        errorBox.appendChild(errorTitle);
        errorBox.appendChild(errorBody);
        errorBox.appendChild(errorHint);
        root.appendChild(errorBox);

        root.appendChild(make('hr', 'sep'));

        const footer = make('div', 'detail-footer');
        const status = make('span', 'detail-status', '');
        footer.appendChild(status);

        const actions = make('div', 'actions');
        // 押した時点の選択を見る。ボタンは作り直さないので、
        // ここで account を閉じ込めると古い ID を送り続けることになる。
        const editButton = makeButton('ghost',
            () => vscode.postMessage({ type: 'editAccount', accountId: selectedId }));
        const reloginButton = makeButton('ghost',
            () => vscode.postMessage({ type: 'relogin', accountId: selectedId }));
        const enableButton = makeButton('ghost', () => {
            const account = currentAccount();
            if (account) {
                vscode.postMessage({
                    type: 'setEnabled', accountId: account.id, enabled: !account.enabled,
                });
            }
        });
        const deleteButton = makeButton('ghost danger',
            () => vscode.postMessage({ type: 'deleteAccount', accountId: selectedId }));
        actions.appendChild(editButton);
        actions.appendChild(reloginButton);
        actions.appendChild(enableButton);
        actions.appendChild(deleteButton);
        footer.appendChild(actions);
        root.appendChild(footer);

        return {
            root, name, source, blocks, hint, errorBox, errorTitle, errorBody, errorHint,
            status, editButton, reloginButton, enableButton, deleteButton, blockNodes: [],
        };
    }

    function updateDetail(view, account) {
        setText(view.name, account.name);
        setText(view.source, account.extraLabel
            ? t('{provider} / {field}: {value}', {
                provider: account.providerLabel,
                field: account.extraLabel,
                value: account.extra || t('(not set)'),
            })
            : account.providerLabel);

        const entry = entryOf(account);
        const info = accountState(account);
        // サマリーと同じく、取得中も前回の値を出し続ける (高さを変えない)
        const metrics = (entry.usage && entry.usage.metrics) || [];

        ensureChildren(view.blocks, view.blockNodes, metrics.length, makeMetricBlock);
        metrics.forEach((metric, index) => updateMetricBlock(view.blockNodes[index], metric));

        setHidden(view.errorBox, entry.state !== 'error');
        if (entry.state === 'error') {
            setText(view.errorTitle, entry.authError
                ? t('The credential has expired')
                : t('Could not fetch usage'));
            setText(view.errorBody, entry.error || '');
            // **どこで何をすればよいかを書きます。** 以前はここが
            // 「別ウィンドウのログイン画面が開きます」と案内していました。
            // その窓はもうありません。取り方の手順は編集の画面に出るので、
            // 行き先はどの取得先でもそこです。
            const note = !entry.authError ? ''
                : t('Press "🔑 Sign in again" below. '
                    + 'The form shows how to get a new {credential}.',
                    { credential: account.credentialLabel });
            setText(view.errorHint, note);
            setHidden(view.errorHint, !note);
        }

        // 案内を出すのは、出せる枠が1つも無いときだけ。前回の値が残っている
        // 間に「取得しています...」を足すと、その分だけ高さが変わる。
        let hint = '';
        if (metrics.length === 0 && entry.state !== 'error') {
            if (entry.state === 'ok') {
                hint = t('There was no metric to show.');
            } else if (!account.enabled) {
                hint = t('This account is disabled.');
            } else if (!account.implemented) {
                hint = t('Fetching usage from this provider is not implemented yet.');
            } else if (account.needsCredential && !account.hasCredential) {
                hint = t('{credential} is not set. Set it from "Edit" below.',
                    { credential: account.credentialLabel });
            } else if (entry.state === 'loading' || entry.state === 'queued') {
                hint = t('Fetching...');
            } else {
                hint = t('Usage has not been fetched yet. Press "Refresh".');
            }
        }
        setText(view.hint, hint);
        setHidden(view.hint, !hint);

        setText(view.status, t('Status: {dot} {note}',
            { dot: info.dot, note: info.note }));
        updateButton(view.editButton, t('Edit'),
            t('Change the registered details'));
        // **資格情報が要る取得先すべてに出します。** 以前は
        // 「ブラウザでログインする取得先」だけに絞っていました。押した先が
        // 別ウィンドウのログイン画面で、API キー方式では開いても何もできな
        // かったためです。いまの行き先は編集のフォームなので、貼り直したい
        // 人には、どの取得先でも意味があります。**コマンドパレット側の
        // 絞り込みと揃えること** (extension.js の relogin)。
        setHidden(view.reloginButton, !account.needsCredential);
        updateButton(view.reloginButton, t('🔑 Sign in again'),
            t('Opens the form for pasting a fresh credential'));
        updateButton(view.enableButton,
            account.enabled ? t('Disable') : t('Enable'),
            account.enabled
                ? t('Leaves this account out of refreshes')
                : t('Puts this account back into refreshes'));
        updateButton(view.deleteButton, t('Delete'),
            t('Removes this account and its saved sign-in state'));
    }

    // ---------------- 全体 ----------------

    /** 作った画面を持ち回す。作り直すのは顔ぶれが変わったときだけ。 */
    // ---------------- 追加・編集のフォーム ----------------
    //
    // **1画面に全部出します。** ここは以前、項目を1つずつ選ばせる形
    // (QuickPick) でした。窓が要らないという点では正しかったのですが、
    // 「いま何が設定されているのか」が一望できず、直すたびにメニューへ
    // 戻ることになりました。登録内容は互いに関係するもの (取得先が決まって
    // 初めて資格情報の意味が決まる) なので、並べて見せるほうが分かります。
    //
    // **資格情報は、こちらへは送られてきません。** 保存済みの値を webview へ
    // 渡さないのは元からの約束です (cli.py の account_payload を参照)。
    // 欄は常に空で開き、入力されたときだけ送ります。空のまま保存すれば
    // 前の値が残ります。
    //
    // **入力中の値は DOM が持ちます。** state は取得のたびに届くので、
    // そこへ入力値を載せると、自動更新が走った瞬間に打ちかけの文字が
    // 消えます。値を流し込むのはフォームを開いた1回だけ (form.token が
    // 変わったとき) で、あとは触りません。

    function makeField(labelText, control) {
        const row = make('div', 'form-row');
        const label = make('label', 'form-label', labelText);
        const id = 'f-' + Math.random().toString(36).slice(2, 9);
        control.id = id;
        label.setAttribute('for', id);
        row.appendChild(label);
        row.appendChild(control);
        const hint = make('div', 'form-hint', '');
        // **説明は入力欄に結び付けます。** 読み上げで使う人には、離れた場所に
        // 置かれた文は入力欄と無関係な位置で読まれます。ここに出るのは
        // 「すでに保存されています。空のままにすれば残ります」という、
        // この画面でいちばん誤解の起きる説明です。
        hint.id = id + '-hint';
        control.setAttribute('aria-describedby', hint.id);
        row.appendChild(hint);
        return { row, label, control, hint };
    }

    function makeForm() {
        const root = make('div', 'form');

        const title = make('h2', 'form-title', '');
        root.appendChild(title);

        // 取得先。**最初に決めます。** これで他の欄の意味が決まります。
        const providerSelect = /** @type {HTMLSelectElement} */
            (make('select', 'form-control'));
        const providerField = makeField(t('Provider'), providerSelect);
        root.appendChild(providerField.row);

        const nameInput = /** @type {HTMLInputElement} */
            (make('input', 'form-control'));
        nameInput.type = 'text';
        const nameField = makeField(t('Name'), nameInput);
        root.appendChild(nameField.row);

        const extraInput = /** @type {HTMLInputElement} */
            (make('input', 'form-control'));
        extraInput.type = 'text';
        const extraField = makeField('', extraInput);
        root.appendChild(extraField.row);

        const budgetInput = /** @type {HTMLInputElement} */
            (make('input', 'form-control'));
        budgetInput.type = 'text';
        // 携帯の数字キーボードは出しますが、**入力は文字のまま扱います。**
        // type="number" にすると、数値として読めない文字 (「1,000」、全角の
        // 数字) を打ったときに value が空文字になり、打った内容が画面に
        // 見えているのに 0 が保存されます。判定は cli.py に任せます。
        budgetInput.inputMode = 'decimal';
        const budgetField = makeField('', budgetInput);
        root.appendChild(budgetField.row);

        // 資格情報。**普段のブラウザで取ってきて、ここへ貼ります。**
        const credentialArea = /** @type {HTMLTextAreaElement} */
            (make('textarea', 'form-control form-credential'));
        credentialArea.rows = 3;
        credentialArea.spellcheck = false;
        const credentialField = makeField('', credentialArea);
        root.appendChild(credentialField.row);

        // 取り方の手順。取得先が持っているものをそのまま出します
        // (どこを開いて何を押すかは取得先の事情で、この画面が知って
        // いてよいことではありません)。
        const manualBox = make('div', 'form-manual');
        const manualSteps = make('pre', 'form-steps', '');
        const manualOpen = makeButton('ghost', () => {
            const provider = currentFormProvider();
            if (provider && provider.manualUrl) {
                vscode.postMessage({ type: 'openManual', url: provider.manualUrl });
            }
        });
        manualBox.appendChild(manualOpen);
        manualBox.appendChild(manualSteps);
        credentialField.row.appendChild(manualBox);

        const enabledInput = /** @type {HTMLInputElement} */
            (make('input', 'form-check'));
        enabledInput.type = 'checkbox';
        const enabledRow = make('div', 'form-row form-row-check');
        const enabledLabel = make('label', 'form-label-inline', t('Enabled'));
        const enabledId = 'f-enabled';
        enabledInput.id = enabledId;
        enabledLabel.setAttribute('for', enabledId);
        enabledRow.appendChild(enabledInput);
        enabledRow.appendChild(enabledLabel);
        root.appendChild(enabledRow);

        // 保存できない理由 / 尋ねられている確認。
        const notice = make('div', 'form-notice');
        // **押した先で何が起きたかを、その場で知らせます。** 保存に失敗しても
        // フォーカスはボタンに残るので、これが無いと読み上げでは何も起きて
        // いないように見えます。
        notice.setAttribute('role', 'alert');
        const noticeText = make('div', 'form-notice-text', '');
        const noticeActions = make('div', 'actions');
        const confirmButton = makeButton('primary', () => submitForm(true));
        noticeActions.appendChild(confirmButton);
        notice.appendChild(noticeText);
        notice.appendChild(noticeActions);
        root.appendChild(notice);

        const footer = make('div', 'form-footer');
        const saveButton = makeButton('primary', () => submitForm(false));
        const cancelButton = makeButton('ghost',
            () => vscode.postMessage({ type: 'cancelForm' }));
        footer.appendChild(saveButton);
        footer.appendChild(cancelButton);
        root.appendChild(footer);

        providerSelect.addEventListener('change', () => {
            // 取得先が変われば、出す欄も入力例も変わります。
            applyFormProvider(views.form);
        });

        // **Esc で閉じられるようにします。** 一覧の行はキーボードだけで
        // 操作できるようにしてあるのに (makeSummaryRow)、この画面だけ
        // 取消しがマウス専用では揃いません。
        root.addEventListener('keydown', (event) => {
            if (event.key === 'Escape' && !cancelButton.disabled) {
                event.preventDefault();
                vscode.postMessage({ type: 'cancelForm' });
            }
        });

        return {
            root, title, providerSelect, providerField, nameInput, nameField,
            extraInput, extraField, budgetInput, budgetField,
            credentialArea, credentialField, manualBox, manualSteps, manualOpen,
            enabledInput, enabledRow, enabledLabel,
            notice, noticeText, confirmButton, saveButton, cancelButton,
            // どのフォームを描いているか。**これが変わったときだけ値を流し込みます。**
            token: null,
            // いま欄に入っている値が、どの取得先のものか。**取得先を変えたら
            // 入れ替えるために持ちます** (applyFormProvider を参照)。
            appliedProvider: null,
        };
    }

    /** フォームの取得先一覧。**必ず配列を返します。** */
    function formProviders() {
        const form = state.form;
        return form && Array.isArray(form.providers) ? form.providers : [];
    }

    /** いま選ばれている取得先の情報 (引けなければ null)。 */
    function currentFormProvider() {
        const view = views.form;
        if (!view) { return null; }
        const id = view.providerSelect.value;
        for (const provider of formProviders()) {
            if (provider.id === id) { return provider; }
        }
        return null;
    }

    /**
     * 取得先に合わせて、出す欄と文言を入れ替えます。
     *
     * **値も入れ替えます。** ラベルだけ差し替えて中身を残すと、前の取得先の
     * 値が次の取得先のものとして保存されます。上限金額なら JPY の 10000 が
     * USD の 10000 になり (桁が2つ違います)、しかも金額に書式の検証は無いので
     * **誰にも気づかれません。** 資格情報も同じで、Claude の Cookie が
     * Azure の API キーとして残ると、一覧では「設定済み」に見えるのに取得は
     * 必ず失敗します。
     */
    function applyFormProvider(view) {
        if (!view) { return; }
        const provider = currentFormProvider();
        if (!provider) {
            // 一覧に無い取得先が選ばれている (設定ファイルを手で書き換えた等)。
            // **欄を消しません。** 消すと、直す手立てがその場から無くなります。
            // ラベルだけ既定の語に戻して、判定はバックエンドに任せます
            // (保存を押せば「知らない取得先です」と返ります)。
            setText(view.extraField.label, t('Provider'));
            setText(view.credentialField.label, t('Provider'));
            setHidden(view.manualBox, true);
            setText(view.credentialField.hint, '');
            return;
        }

        // **開いた直後は入れ替えません。** そこに入っているのは、まさにこの
        // 取得先の値です。
        if (view.appliedProvider !== null
                && view.appliedProvider !== provider.id) {
            view.extraInput.value = '';
            view.budgetInput.value = provider.supportsBudget
                ? String(provider.defaultBudget || '') : '';
            view.credentialArea.value = '';
        }
        view.appliedProvider = provider.id;

        setHidden(view.extraField.row, !provider.usesExtraField);
        setText(view.extraField.label, provider.extraLabel || '');
        view.extraInput.placeholder = provider.extraHint || '';

        setHidden(view.budgetField.row, !provider.supportsBudget);
        setText(view.budgetField.label, provider.currency
            ? t('Spending cap ({currency})', { currency: provider.currency })
            : t('Spending cap'));

        setHidden(view.credentialField.row, !provider.needsCredential);
        setText(view.credentialField.label, provider.credentialLabel || '');
        view.credentialArea.placeholder = provider.credentialHint || '';

        // 手順を持っている取得先だけ、開くボタンと手順を出します。
        setHidden(view.manualBox, !provider.manualUrl);
        setText(view.manualOpen, t('Open {provider} in your browser',
            { provider: provider.label }));
        setText(view.manualSteps, provider.manualSteps || '');

        // 「すでに保存されています」と言えるのは、**開いたときの取得先の
        // ままでいる**ときだけです。取得先を変えたなら、保存されているものは
        // もうこの取得先のものではありません (cli.py の update_account が、
        // 取得先を変えた保存では資格情報を求め直します)。
        const form = state.form;
        const kept = form && form.mode === 'edit' && form.hasCredential
            && form.values && form.values.provider === provider.id;
        setText(view.credentialField.hint, kept
            ? t('One is already saved. Leave this empty to keep it as it is.')
            : '');
    }

    function updateForm(view, form) {
        // **値を流し込むのは開いた1回だけ。** 毎回入れ直すと、打っている
        // 最中に自動更新が走った瞬間、入力が消えます。
        if (view.token !== form.token) {
            view.token = form.token;
            // **壊れた形で届いても、この画面が使えなくなるだけで済ませます。**
            // ここで例外を投げると message の受け口の外まで抜け、以後 state が
            // 届いても描き直されません (画面全体が固まります)。
            const list = Array.isArray(form.providers) ? form.providers : [];
            const values = form.values || {};

            view.providerSelect.replaceChildren();
            for (const provider of list) {
                const option = make('option', '', provider.label);
                option.value = provider.id;
                view.providerSelect.appendChild(option);
            }
            view.providerSelect.value = values.provider || '';
            // 取得先を変えると資格情報の意味が変わるので、既存アカウントの
            // 取得先はそのまま選べるようにしてあります (取り下げたものを
            // 使っていても、その項目は一覧に入っています)。

            view.nameInput.value = values.name || '';
            view.extraInput.value = values.extra || '';
            view.budgetInput.value = values.budget ? String(values.budget) : '';
            view.credentialArea.value = '';
            view.enabledInput.checked = values.enabled !== false;
            // **いま入っている値が、どの取得先のものか。** これを先に立てて
            // おかないと、開いた直後の applyFormProvider が「取得先が変わった」
            // と判断して、読み込んだばかりの値を消します。
            view.appliedProvider = values.provider || null;

            setText(view.title, form.mode === 'add'
                ? t('Add Account') : t('Edit Account'));
            setText(view.saveButton, form.mode === 'add' ? t('Add') : t('Save'));
            setText(view.cancelButton, t('Cancel'));
            setText(view.confirmButton, t('Save anyway'));
            setText(view.nameField.hint, t('The name shown in the list'));
            applyFormProvider(view);
            view.nameInput.focus();
        }

        // ここから下は、届くたびに反映してよいもの。
        const busy = Boolean(form.busy);
        view.saveButton.disabled = busy;
        view.cancelButton.disabled = busy;
        view.providerSelect.disabled = busy;
        // **確認のボタンも止めます。** ここが抜けていると、確認が出ている
        // 状態で素早く2回押せてしまい、追加なら同じアカウントが2件できます。
        view.confirmButton.disabled = busy;

        const confirm = form.confirm && form.confirm.length ? form.confirm : null;
        const message = form.error || (confirm ? confirm.join('\n\n') : '');
        setHidden(view.notice, !message);
        setText(view.noticeText, message);
        // 確認は「承知のうえで保存する」を押せば通せます。保存できない理由
        // (名前が空、など) は押しても通らないので、ボタンを出しません。
        setHidden(view.confirmButton, !confirm);
        view.notice.classList.toggle('is-error', Boolean(form.error));
    }

    /** 入力された内容を送ります。 */
    function submitForm(confirmed) {
        const view = views.form;
        const form = state.form;
        if (!view || !form) { return; }
        // **押した直後にもう一度押されても、送るのは1回だけ。** disabled は
        // 応答が返ってくるまで立たないので、その往復の間に窓が開きます。
        if (form.busy) { return; }

        const values = {
            provider: view.providerSelect.value,
            name: view.nameInput.value,
            enabled: view.enabledInput.checked,
        };
        // **画面に出ている欄だけを送ります。** 取得先が引けないときでも
        // 送るのをやめてはいけません。押しても何も起きないボタンになり、
        // 直す手立てがその場から無くなります。載せなかったキーは「触らない」
        // という意味なので、隠れている欄は既定へ戻されません
        // (cli.py の update_account)。
        if (!view.extraField.row.classList.contains('hidden')) {
            values.extra = view.extraInput.value;
        }
        if (!view.budgetField.row.classList.contains('hidden')) {
            // **打った文字をそのまま渡します。** 数値へ直すのはここでは
            // ありません。空欄だけが 0 (上限なし) で、それ以外の
            // 「数値として読めないもの」は cli.py が理由を返します。
            const typed = view.budgetInput.value.trim();
            values.budget = typed === '' ? 0 : typed;
        }
        // **空なら送りません。** 送れば「空にしてください」という指示に
        // なり、保存済みの資格情報が消えます (cli.py の update_account)。
        const credential = view.credentialArea.value.trim();
        if (credential) { values.credential = credential; }

        vscode.postMessage({
            type: 'saveAccount',
            mode: form.mode,
            accountId: form.accountId,
            values: values,
            confirmed: Boolean(confirmed),
        });
    }

    const views = { summary: null, detail: null, form: null, mode: null };

    function currentAccount() {
        if (!selectedId) { return null; }
        return state.accounts.find((a) => a.id === selectedId) || null;
    }

    function persist() {
        vscode.setState({ selectedId: selectedId });
    }

    function render() {
        // 選んでいたアカウントが消えていたらサマリーへ戻る
        // (どちらのパネルも出ていない空白の画面を作らない)。
        //
        // **一覧が届く前に判断しないこと。** 画面を作り直した直後は accounts が
        // 空なので、そこで「消えた」と決めつけると、閉じて開くたびに選択が
        // サマリーへ戻ってしまいます。
        if (selectedId && state.loaded && !currentAccount()) {
            selectedId = null;
            persist();
        }
        const account = currentAccount();
        // フォームを開いている間は、それを最優先で出します。**入力の途中で
        // 自動更新が走っても画面が切り替わってはいけません。**
        const mode = state.form ? 'form' : (account ? 'detail' : 'summary');

        // 器を入れ替えるのは、行き来したときだけ。
        if (views.mode !== mode) {
            if (views.mode === 'form' && views.form) {
                // **フォームから離れるときに、貼られたものを消します。**
                // 器は捨てずに取っておくので (下の makeForm は1度きり)、
                // 消さないと、利用者が貼った Cookie や API キーが切り離された
                // DOM に残り続けます。ここは DevTools で中を覗ける場所です。
                // token も一緒に戻し、次に開いたときは必ず入れ直させます。
                views.form.credentialArea.value = '';
                views.form.token = null;
                views.form.appliedProvider = null;
            }
            if (mode === 'form') {
                if (!views.form) { views.form = makeForm(); }
                el.host.replaceChildren(views.form.root);
            } else if (mode === 'summary') {
                if (!views.summary) { views.summary = makeSummary(); }
                el.host.replaceChildren(views.summary.root);
            } else {
                if (!views.detail) { views.detail = makeDetail(); }
                el.host.replaceChildren(views.detail.root);
            }
            views.mode = mode;
        }

        if (mode === 'form') {
            updateForm(views.form, state.form);
        } else if (mode === 'summary') {
            updateSummary(views.summary);
        } else {
            updateDetail(views.detail, account);
        }

        el.tabSummary.classList.toggle('active', !account);
        setHidden(el.tabDetailWrap, !account);
        if (account) {
            setText(el.tabDetail, account.name);
            el.tabDetail.classList.add('active');
        }

        renderToolbar(account);
        renderStatus();
    }

    /**
     * 「更新」ボタンは**今その画面に出ているものを更新する**ボタン。
     *
     * サマリーは全アカウントを出している画面なので、そこでは全件更新になります。
     * ここを無効にすると、サマリーを開きっぱなしにする使い方 (このツールの
     * 主用途) で主要ボタンが一度も押せなくなります。
     */
    function renderToolbar(account) {
        const pending = Object.keys(state.entries).filter((id) => {
            const s = state.entries[id].state;
            return s === 'loading' || s === 'queued';
        }).length;
        const busy = state.refreshing || pending > 0;

        if (account) {
            const canFetch = state.fetchable.indexOf(account.id) >= 0;
            el.refresh.disabled = busy || !canFetch;
            el.refresh.title = canFetch
                ? t('Refreshes this account')
                : t('This account cannot be refreshed right now '
                    + '(disabled, not implemented, or missing its credential)');
        } else {
            el.refresh.disabled = busy || state.accounts.length === 0;
            el.refresh.title = t('Refreshes every account shown');
        }
        setText(el.refresh, busy ? t('Refreshing...') : t('Refresh'));

        setHidden(el.progress, !(busy && pending > 0));
        if (busy && pending > 0) {
            // 単数と複数で鍵を分けます。対応する4言語のうち数で形が変わるのは
            // 英語だけなので、Intl.PluralRules を持ち出すほどのことはありません。
            setText(el.progress, pending === 1
                ? t('1 left')
                : t('{count} left', { count: pending }));
        }

        // **いま何順で並んでいるかを、押す前に見せます。** title だけだと
        // ホバーしなければ読めないので、基準で並べているあいだは ⇅ 自体を
        // 目立たせます。手で並べた順のままなら、素のままにしておきます。
        const sort = currentSort();
        // **機能名を落としません。** 状態名だけにすると、押すと何が起きる
        // ボタンなのかがツールチップから読み取れなくなります (🌐 と ⚙ は
        // 機能名を出しています)。新しい原文は足さず、既にある2つを繋ぎます。
        el.sort.title = t('Sort') + ': ' + sortLabel(sort);
        el.sort.classList.toggle('active', sort !== 'manual');
    }

    function renderStatus() {
        if (state.loadError) {
            setText(el.status,
                t('Cannot read the settings file: {reason}', { reason: state.loadError }));
            return;
        }
        const parts = [];
        if (state.accounts.length > 0) {
            const counts = {};
            for (const account of state.accounts) {
                const dot = accountState(account).dot;
                counts[dot] = (counts[dot] || 0) + 1;
            }
            const order = ['🔴', '🟡', '🟢', '🔵', '⚫', '⚪'];
            const breakdown = order.filter((d) => counts[d]).map((d) => d + counts[d]).join(' ');
            parts.push(state.accounts.length === 1
                ? t('1 account  {breakdown}', { breakdown: breakdown })
                : t('{count} accounts  {breakdown}',
                    { count: state.accounts.length, breakdown: breakdown }));
        }
        if (!state.encryptionAvailable) {
            parts.push(t('⚠ Credentials are stored unencrypted'));
        }
        setText(el.status, parts.join('   |   '));
    }

    /**
     * 残り時間だけを1秒ごとに書き換えます。
     *
     * 画面を作り直すと、押している最中のボタンやスクロール位置が飛びます。
     */
    function tickCountdowns() {
        const nodes = document.querySelectorAll('[data-resets-at]');
        for (let i = 0; i < nodes.length; i++) {
            const node = /** @type {HTMLElement} */ (nodes[i]);
            setText(node, remainingText(node.dataset.resetsAt));
        }
    }

    // ---------------- 拡大縮小 ----------------
    //
    // **この画面だけを拡大します** (VSCode 全体ではありません)。倍率は
    // document.body の zoom に入れるので、px で決め打ちにした余白もゲージの
    // 高さも角の丸みも、CSS を1行も変えずに一緒に付いてきます。**寸法を
    // 個別に換算する方式は採りません** — main.css には追随させるべき px が
    // 80 箇所ほどあり、1つ取りこぼすと「文字だけ大きくなって余白が付いて
    // こない」という、高倍率にしないと気づけない壊れ方をします。

    /**
     * 選べる倍率 (%)。
     *
     * 段を決め打ちにしているのは、1% ずつ動かせても使い道が無いからです。
     * ホイールを一度回すごとに、見て分かるだけ変わってほしいのです。
     */
    const ZOOM_STEPS = [50, 67, 80, 90, 100, 110, 125, 150, 175, 200];

    /**
     * いまの倍率。**panel.js が HTML に埋めた値から始めます**
     * (window.__zoom)。
     *
     * **状態 (post) では受け取りません。** あちらは取得のたびに十数回飛ぶので、
     * ホイールで連続的に変えている最中に古い値が届いて倍率が跳ねます。
     * 一方向 (開くときに1回もらい、以後はこちらが持ち主) にすれば、
     * その競合が原理的に起きません。
     */
    let zoom = clampZoom(window.__zoom);

    /** @type {number|null} 設定への書き込みを落ち着くまで待つタイマー。 */
    let zoomSaveTimer = null;

    function clampZoom(value) {
        // **数かどうかを先に見ます。** Number(null) と Number('') は 0 なので、
        // isFinite だけでは素通りして下限の 50% に丸められます。設定ファイルは
        // 人が直接書き換えられる場所なので、意味を成さない値は等倍へ倒します。
        if (typeof value !== 'number' || !isFinite(value)) { return 100; }
        return Math.max(ZOOM_STEPS[0],
            Math.min(ZOOM_STEPS[ZOOM_STEPS.length - 1], Math.round(value)));
    }

    /**
     * いまの倍率から1段ぶん動かした倍率を返します。
     *
     * **段の間の値からでも動きます。** 設定ファイルは人が直接書き換えられる
     * ので、137 のような値が入っていることがあります。「いまの値より大きい/
     * 小さい最初の段」を選ぶことで、そこからでも1回目の ＋/− が効きます。
     */
    function steppedZoom(delta) {
        if (delta > 0) {
            for (let i = 0; i < ZOOM_STEPS.length; i++) {
                if (ZOOM_STEPS[i] > zoom) { return ZOOM_STEPS[i]; }
            }
            return ZOOM_STEPS[ZOOM_STEPS.length - 1];
        }
        for (let i = ZOOM_STEPS.length - 1; i >= 0; i--) {
            if (ZOOM_STEPS[i] < zoom) { return ZOOM_STEPS[i]; }
        }
        return ZOOM_STEPS[0];
    }

    /**
     * 倍率を画面に当てます。**変わったときだけ書きます** (setText と同じ理由)。
     *
     * **スクロール位置には触りません。** 実測したところ (Chromium 149)、
     * .content の scrollTop は zoom の外側の単位で、倍率を変えても値も
     * 「画面の上端に出ている行」も変わりませんでした。ここで倍率比を掛けて
     * 補正すると、かえって見ていた行が画面外へ飛びます (100% → 200% で
     * 2倍にすると、3行ぶん下へ送られました)。**足さないでください。**
     *
     * @param {number} next 当てたい倍率 (%)
     * @param {boolean} save 設定へ書き戻すか
     */
    function applyZoom(next, save) {
        const value = clampZoom(next);
        // **上限・下限に張り付いているときは何もしません。** ここを素通しに
        // すると、200% でホイールを回し続けている間ずっと settings.json へ
        // 同じ値を書き続けることになります。
        if (value === zoom) { return; }
        zoom = value;
        document.body.style.zoom = String(zoom / 100);
        renderZoom();
        if (save) { scheduleZoomSave(); }
    }

    function renderZoom() {
        setText(el.zoomLevel, t('{percent}%', { percent: zoom }));
        el.zoomOut.disabled = zoom <= ZOOM_STEPS[0];
        el.zoomIn.disabled = zoom >= ZOOM_STEPS[ZOOM_STEPS.length - 1];
    }

    /** 設定への書き込みは落ち着いてから1回だけ (ホイールは連続で来ます)。 */
    function scheduleZoomSave() {
        if (zoomSaveTimer !== null) { clearTimeout(zoomSaveTimer); }
        zoomSaveTimer = setTimeout(flushZoomSave, 400);
    }

    /**
     * 待っている書き込みを、いますぐ出します。
     *
     * **画面が消える前に必ず呼びます。** 言語を切り替えると拡張ホストが
     * webview を作り直すので (panel.js の refreshHtml)、倍率を変えてから
     * 400ms 以内に言語を変えると、その倍率は保存されないまま消えます。
     */
    function flushZoomSave() {
        if (zoomSaveTimer === null) { return; }
        clearTimeout(zoomSaveTimer);
        zoomSaveTimer = null;
        vscode.postMessage({ type: 'setZoom', percent: zoom });
    }

    // ---------------- 配線 ----------------

    el.refresh.addEventListener('click', () => {
        const account = currentAccount();
        if (account) {
            vscode.postMessage({ type: 'refreshOne', accountId: account.id });
        } else {
            vscode.postMessage({ type: 'refreshAll' });
        }
    });

    el.tabSummary.addEventListener('click', () => {
        selectedId = null;
        persist();
        render();
    });

    // 設定は取得中でもログイン中でも開けるようにする (更新間隔や Python の
    // 指定を直したいのは、たいてい何かがうまくいっていないとき)。
    el.settings.addEventListener('click', () => {
        vscode.postMessage({ type: 'openSettings' });
    });

    // 言語だけツールバーに出しているのは、**読めない言語で表示されている状態から
    // 設定画面を探し当てるのが難しい**ため。選ばせるのは拡張ホスト側です
    // (webview からは設定を書けません)。
    el.language.addEventListener('click', () => {
        vscode.postMessage({ type: 'selectLanguage' });
    });

    // 並べ方も、選ばせるのは拡張ホスト側です (言語と同じ理由 — webview からは
    // 設定を書けません)。選ばれた結果は状態に乗って戻ってきます。
    el.sort.addEventListener('click', () => {
        vscode.postMessage({ type: 'selectSort' });
    });

    el.zoomOut.addEventListener('click', () => applyZoom(steppedZoom(-1), true));
    el.zoomIn.addEventListener('click', () => applyZoom(steppedZoom(+1), true));
    // 「100%」の表示そのものが、等倍へ戻すボタンです。Ctrl+0 は VSCode 本体が
    // 別の用途 (サイドバーへ移動) に使っているので、割り当てていません。
    el.zoomLevel.addEventListener('click', () => applyZoom(100, true));

    // **{ passive: false } が要ります。** window に付けた wheel は既定で
    // passive 扱いになり、preventDefault() が効きません。効かないと、
    // Chromium 自身の iframe ズームが同時に走ります。
    //
    // ここで完結させられるのは、VSCode が webview 内の Ctrl+ホイールを
    // 取っていないためです (webview のプリロードはホストへ ctrlKey を
    // 送っておらず、editor.mouseWheelZoom も既定で無効です)。キーボードの
    // ほうは事情が違います (panel.js の nudgeZoom を参照)。
    window.addEventListener('wheel', (event) => {
        if (!event.ctrlKey) { return; }
        // **縦に回っていないものは倍率を動かしません。** 下の三項は deltaY が
        // 0 のとき「縮小」に落ちるので、トラックパッドの横方向で Ctrl を押して
        // いるだけで縮みます。preventDefault より先に返して、横スクロールその
        // ものは邪魔しないでおきます。
        if (!event.deltaY) { return; }
        event.preventDefault();
        applyZoom(steppedZoom(event.deltaY < 0 ? +1 : -1), true);
    }, { passive: false });

    // 画面が隠される/捨てられるときに、待っている書き込みを出します。
    document.addEventListener('visibilitychange', () => {
        if (document.hidden) {
            flushZoomSave();
            endDrag(false);
        }
    });
    window.addEventListener('pagehide', () => flushZoomSave());

    // **掴んだまま画面から離れられます。** 落とすまで必ず完走するという
    // 前提には立てないので、取り消される道を全部塞いでおきます。
    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape' && dragging) {
            event.preventDefault();
            endDrag(false);
        }
    });
    window.addEventListener('blur', () => endDrag(false));

    // **掴みが終わったあとの click を1つだけ捨てます** (swallowNextClick の
    // 説明を参照)。捨てるのは window の捕まえ段階で、行に届く前です。
    window.addEventListener('click', (event) => {
        if (!swallowNextClick) { return; }
        swallowNextClick = false;
        event.stopPropagation();
    }, true);
    // **押し直したら、もう捨てません。** click は必ず pointerdown のあとに
    // 来るので、click が来ないまま終わったとき (行の外で離した等) の消し忘れ
    // が、次の関係のない click を飲み込むことはありません。
    window.addEventListener('pointerdown', () => { swallowNextClick = false; }, true);

    window.addEventListener('message', (event) => {
        const message = event.data;
        if (!message) { return; }
        if (message.type === 'state') {
            // **状態は必ず受け取ります。** 捨てると取得結果を1回分落とします。
            state = message;
            // 先に描いたものを捨てるかどうかは、掴んでいるかとは関係ありません。
            releaseLocalOrder(message);
            // **掴んでいるあいだは描き直しだけを止めます** (renderPending の
            // 説明を参照)。離したときに endDrag が追いつかせます。
            if (dragging) {
                renderPending = true;
                return;
            }
            render();
            return;
        }
        // 並び順を保存できなかったという知らせ (panel.js の case
        // 'reorderAccounts')。**推測では立てません。** これが無いと、画面は
        // 直後に届く状態を「保存の応答より先に来た自動更新」と区別できず、
        // 先に描いたぶんを捨てられません。
        if (message.type === 'orderFailed') {
            orderSaveFailed = true;
            return;
        }
        // 拡大縮小のキー操作だけは拡張ホストから届きます (panel.js の
        // nudgeZoom)。**キーは webview では拾えません。**
        if (message.type === 'zoom') {
            applyZoom(steppedZoom(Number(message.delta) < 0 ? -1 : +1), true);
        }
    });

    setInterval(tickCountdowns, 1000);

    // **描く前に当てます。** あとから当てると、等倍で1度描いたものが
    // 目の前で伸び縮みするのが見えます。
    document.body.style.zoom = String(zoom / 100);
    renderZoom();

    render();
    vscode.postMessage({ type: 'ready' });
})();
