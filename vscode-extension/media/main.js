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
        accounts: [], entries: {}, fetchable: [], refreshing: false, guiBusy: null,
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
        banner: /** @type {HTMLElement} */ (document.getElementById('banner')),
        bannerText: /** @type {HTMLElement} */ (document.getElementById('banner-text')),
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

    /**
     * 操作ボタン。ログイン画面を伴う操作が走っている間は必ず止めます。
     * 2つ開くと、同じ設定ファイルを両方が書き戻して片方の変更が消えます。
     */
    function makeButton(className, onClick) {
        const button = /** @type {HTMLButtonElement} */ (make('button', className, ''));
        button.type = 'button';
        button.addEventListener('click', onClick);
        return button;
    }

    function updateButton(button, label, tooltip) {
        setText(button, label);
        button.title = state.guiBusy
            ? t('{operation} is in progress', { operation: state.guiBusy })
            : tooltip;
        button.disabled = Boolean(state.guiBusy);
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
            t('Opens the sign-in window to register an account'));
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
            const note = !entry.authError ? ''
                : account.canRelogin
                    ? t('Press "🔑 Sign in again" below to open the sign-in window.')
                    : t('Get a new {credential} and set it from "Edit".',
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
            t('Change the registered details (you can open the sign-in window too)'));
        // ブラウザでログインする取得先だけの機能。API キー方式の取得先に
        // 出すと、押しても何も起きないボタンになる。
        setHidden(view.reloginButton, !account.canRelogin);
        updateButton(view.reloginButton, t('🔑 Sign in again'),
            t('Opens the sign-in window to get fresh cookies'));
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
    const views = { summary: null, detail: null, mode: null };

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
        const mode = account ? 'detail' : 'summary';

        // 器を入れ替えるのは、サマリーと詳細を行き来したときだけ。
        if (views.mode !== mode) {
            if (mode === 'summary') {
                if (!views.summary) { views.summary = makeSummary(); }
                el.host.replaceChildren(views.summary.root);
            } else {
                if (!views.detail) { views.detail = makeDetail(); }
                el.host.replaceChildren(views.detail.root);
            }
            views.mode = mode;
        }

        if (mode === 'summary') {
            updateSummary(views.summary);
        } else {
            updateDetail(views.detail, account);
        }

        // ログイン画面は別ウィンドウなので、VSCode を見ている限り
        // 「押したのに何も起きない」ように見える。どこを見ればよいか出す。
        setHidden(el.banner, !state.guiBusy);
        if (state.guiBusy) {
            setText(el.bannerText, t(
                'Opening a window for {operation}. '
                + 'Finish there and the result shows up here.',
                { operation: state.guiBusy }));
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
            el.refresh.disabled = busy || !canFetch || Boolean(state.guiBusy);
            el.refresh.title = canFetch
                ? t('Refreshes this account')
                : t('This account cannot be refreshed right now '
                    + '(disabled, not implemented, or missing its credential)');
        } else {
            el.refresh.disabled = busy || state.accounts.length === 0 || Boolean(state.guiBusy);
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
