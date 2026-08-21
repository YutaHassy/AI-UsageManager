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
        language: /** @type {HTMLButtonElement} */ (document.getElementById('language')),
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

        const header = make('div', 'summary-header');
        const dot = make('span', 'dot', '');
        const name = make('span', 'summary-name', '');
        header.appendChild(dot);
        header.appendChild(name);
        root.appendChild(header);

        const lines = make('div', 'summary-lines');
        root.appendChild(lines);

        const open = () => { selectedId = accountId; persist(); render(); };
        root.addEventListener('click', open);
        root.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); }
        });

        return { root, dot, name, lines, lineNodes: [] };
    }

    function updateSummaryRow(row, account) {
        const info = accountState(account);
        setText(row.dot, info.dot);
        setText(row.name, t('{name} [{provider}]',
            { name: account.name, provider: account.providerLabel }));

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

        const actions = make('div', 'actions');
        const addButton = makeButton('ghost', () => vscode.postMessage({ type: 'addAccount' }));
        actions.appendChild(addButton);
        root.appendChild(actions);

        return { root, list, empty, hint, actions, addButton, rows: new Map() };
    }

    function updateSummary(view) {
        const accounts = state.accounts;

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
        setHidden(view.empty, hasAccounts);
        setText(view.empty, state.loaded
            ? t('No accounts yet. Use "＋ Add" to register one.')
            : t('Loading...'));
        setHidden(view.actions, !state.loaded);
        updateButton(view.addButton, t('＋ Add'),
            t('Opens the form for registering an account'));
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

    window.addEventListener('message', (event) => {
        const message = event.data;
        if (message && message.type === 'state') {
            state = message;
            render();
        }
    });

    setInterval(tickCountdowns, 1000);

    render();
    vscode.postMessage({ type: 'ready' });
})();
