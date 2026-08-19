/**
 * Python バックエンドとの会話。
 *
 * ビルド工程を持たない素の CommonJS。require するのは 'vscode' と Node 組み込みだけで、
 * node_modules は使わない。**このファイルがそのまま vsix に入る原本です。**
 */

'use strict';

const cp = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const vscode = require('vscode');

const { language, t } = require('./i18n');

/**
 * 1本目の Python で起動できなかったとき、Python 拡張の答えを待つ上限 (ミリ秒)。
 *
 * 実測で 4.3 秒／18.4 秒。**成功パスでは1ミリ秒も払いません** — ここを通るのは
 * すでに起動に失敗したときだけです。15 秒にしているのは、よくある側 (4 秒台) を
 * 確実に拾いつつ、「起動できませんでした」を利用者へ返すまでの遅れを、
 * ログイン画面の案内 (extension.js の GUI_STUCK_HINT_MS = 20 秒) より短く保つ
 * ためです。取り逃がしても pythonExtPromise は残るので、次の操作 (更新ボタン)
 * では待ち時間ゼロで拾えます。
 */
const PYTHON_EXT_FALLBACK_MS = 15000;

/**
 * バックエンド (Python) が返した失敗。
 *
 * authError は「資格情報が切れている」ことを表します。ふつうの取得失敗と
 * 区別するのは、利用者に見せる次の一手が違うためです (再ログインが要る)。
 */
class BackendError extends Error {
    /**
     * @param {string} message
     * @param {boolean} authError
     */
    constructor(message, authError) {
        super(message);
        this.authError = authError;
        this.name = 'BackendError';
    }
}

/**
 * 返事を待っている要求ひとつ。
 *
 * @typedef {object} Pending
 * @property {(value: any) => void} resolve
 * @property {(reason: Error) => void} reject
 */

/**
 * 画面に出せる形にしたアカウント1件。
 *
 * **資格情報そのものは入っていません** (cli.py の account_payload 参照)。
 * 画面が必要とするのは「設定済みかどうか」だけです。
 *
 * @typedef {object} AccountInfo
 * @property {string} id
 * @property {string} name
 * @property {string} provider
 * @property {string} providerLabel
 * @property {boolean} enabled
 * @property {boolean} implemented この取得先の使用状況取得が実装済みか
 * @property {boolean} canRelogin ブラウザでのログインに対応しているか
 * @property {string} credentialLabel 「セッションキー」「API キー」など
 * @property {boolean} needsCredential
 * @property {boolean} hasCredential
 * @property {string} extraLabel 組織 ID などの追加項目の名前 (無ければ空)
 * @property {string} extra
 * @property {number} budget 利用者が決めた上限金額 (0 で未設定)
 */

/**
 * 設定ファイルを読んだ結果ひとまとまり。
 *
 * @typedef {object} Snapshot
 * @property {AccountInfo[]} accounts
 * @property {string[]} fetchable 今この場で取得を試せるアカウントの ID
 * @property {string} configPath
 * @property {string} configDir
 * @property {string|null} loadError 設定ファイルを読めなかった理由
 * @property {boolean} encryptionAvailable 資格情報を暗号化して保存できるか
 * @property {boolean} [cancelled] ログイン画面を伴う操作の応答にだけ載る。
 *   利用者が中止したか (cli.py の gui_operation 参照)。**いまの JS はこれを
 *   読んでいません** — 中止の判定は extension.js の runGuiCommand が
 *   token.onCancellationRequested で持っています。
 * @property {string} [accountId] 同上。追加・編集の対象になったアカウント。
 */

/**
 * Python バックエンドとの会話を受け持ちます。
 *
 * 標準出力は1行1 JSON のプロトコル専用です。ログは標準エラーに出るので、
 * そちらは丸ごと出力チャンネルへ流します (混ぜるとどちらも読めなくなる)。
 *
 * @implements {vscode.Disposable}
 */
class Backend {
    /**
     * @param {string} extensionPath
     * @param {vscode.OutputChannel} log
     */
    constructor(extensionPath, log) {
        this.extensionPath = extensionPath;
        this.log = log;

        /** @type {cp.ChildProcessWithoutNullStreams|undefined} */
        this.process = undefined;
        /** @type {Map<number, Pending>} */
        this.pending = new Map();
        this.nextId = 1;
        /** @type {Promise<void>|undefined} */
        this.starting = undefined;
        this.disposed = false;
        /**
         * 何本目の起動か。
         *
         * **引き直し (start) が「自分はもう用済みか」を知るために持ちます。**
         * 起動の途中で kill() / restart() / dispose() が入ることがあり
         * (画面が出ないときに利用者が「バックエンドを再起動」を押す、が典型)、
         * そのとき待っている start() には「自分の spawn が拒否された」ことしか
         * 分かりません。番号を持たないとそれを「1本目の Python が悪かった」と
         * 読み違え、最大 15 秒待ったあとで**他人が起こした生きたプロセスを
         * 殺しに行きます**。そこまで進むと待機中の要求が永久に返らなくなります。
         */
        this.startGeneration = 0;
        /**
         * バックエンドが「もう動けない」と言ってきた理由。再起動まで保持する。
         * @type {string|undefined}
         */
        this.fatalMessage = undefined;
        /**
         * Python 拡張の activate を待つ約束。**起動の待ち行列には入れません。**
         * 1本目の Python で起動できなかったときにだけ、ここを覗きます。
         * @type {Promise<string>|undefined}
         */
        this.pythonExtPromise = undefined;

        /** @type {vscode.EventEmitter<string>} */
        this._onFetching = new vscode.EventEmitter();
        /** ある口座の取得が実際に始まった (キューから出た) タイミング。 */
        this.onFetching = this._onFetching.event;

        /** @type {vscode.EventEmitter<void>} */
        this._onGuiStarted = new vscode.EventEmitter();
        /**
         * ログイン画面のプロセスが起動した。
         *
         * **ウィンドウが出たことの保証ではありません。** バックエンドに
         * 分かるのは子プロセスを起こしたところまでで、実際に窓を作らないまま
         * 固まる例が出ています。それでも合図が要るのは、これが来るまでは
         * 「別ウィンドウで操作してください」と案内すべきではないためです。
         */
        this.onGuiStarted = this._onGuiStarted.event;
    }

    dispose() {
        this.disposed = true;
        this._onFetching.dispose();
        this._onGuiStarted.dispose();
        this.kill();
    }

    kill() {
        for (const [, pending] of this.pending) {
            pending.reject(new BackendError(t('The backend has stopped.'), false));
        }
        this.pending.clear();
        this.process?.kill();
        this.process = undefined;
        this.starting = undefined;
        // **番号を進めます。** 起動の途中でここへ来ることがあり、待っている
        // start() はそれを知る手段を他に持ちません。進めておけば、あちらは
        // 自分の spawn の拒否を「自分のせいではない」と見分けられます。
        this.startGeneration++;
    }

    /** 設定変更などで作り直したいときに使います。 */
    restart() {
        this.log.appendLine('[backend] 再起動します。');
        this.fatalMessage = undefined;
        // Python 拡張の答えも聞き直します。**作り直す理由はたいてい
        // 「Python を変えた」です** (extension.js は pythonPath の変更で
        // ここを呼びます)。前に聞いた答えを握ったままだと、引き直しのときに
        // 古いインタープリタへ戻してしまいます。すでに active なら聞き直しは
        // ほぼ一瞬で終わるので、捨てる側に倒します。
        this.pythonExtPromise = undefined;
        this.kill();
    }

    // ---------------- Python の場所 ----------------

    /**
     * 設定 `aiUsageManager.pythonPath` の値 (前後の空白を落としたもの)。
     *
     * 未設定なら空文字。**「利用者が明示的に指定したか」の判定にも使います** —
     * 指定があるなら、こちらが勝手に別の Python で引き直してはいけません。
     *
     * @returns {string}
     */
    configuredPython() {
        return vscode.workspace
            .getConfiguration('aiUsageManager')
            .get('pythonPath', '')
            .trim();
    }

    /**
     * バックエンドを動かす Python を決めます。
     *
     * **設定が最優先です。** 自動検出は「たいてい当たる」程度のものなので、
     * 外した場合に利用者が上書きできる余地を必ず残します。
     *
     * **ここでは待ちません。** 外した場合の埋め合わせは start() の引き直しが
     * 持ちます (下の Python 拡張のブロックを参照)。
     *
     * @returns {Promise<string>}
     */
    async resolvePython() {
        const t0 = Date.now();
        /** @type {string} */
        let python = this.configuredPython();

        // Python 拡張が選んでいるインタープリタ。利用者が VSCode 上で
        // 明示的に選んだものなので、こちらの当てずっぽうより信用できる。
        //
        // **activate() を待ちません。** 待つと、この拡張の起動がまるごと Python
        // 拡張の初期化に引きずられます (実測 4.3 秒／18.4 秒。Python 拡張のログに
        // "Native locator: Refresh finished in 15778 ms" が出ます)。resolvePython は
        // spawn から直列に呼ばれるので、ここで待った時間はそのまま
        // 「画面に何も出ない時間」になります。
        //
        // **信用の順序は変えていません。** すでに active なら、これまでどおり
        // Python 拡張の答えを最優先で使います。まだなら裏で activate を始めておき、
        // こちらは当てずっぽう (venv -> PATH) で先に進みます。**当てずっぽうが
        // 外れたときは start() が Python 拡張の答えを待って一度だけ引き直します。**
        // これが無いと、Python 拡張だけが知っている環境 (conda 等) に requests を
        // 入れている利用者が、設定を手で書くまで永久に起動できなくなります。
        //
        // 採らなかった案:
        //   - activate() に短いタイムアウトを付ける: 実測 4.3〜18.4 秒なので、
        //     効果が出るほど短くする (1.5 秒等) と実質「待たない」と同じで
        //     成功率だけ落ち、間に合う長さ (20 秒) にすると何も改善しません。
        //     両者の悪いところ取りです。
        //   - Python 拡張を一切見ない: venv 利用者に恒久的な退行。復旧手段が
        //     「設定を手で書く」だけになります。
        //   - 裏で解決して次回以降に活かすだけ (失敗時の引き直し無し):
        //     バックエンドは長命プロセスなので「次回」が来ません。
        //     **引き直しとセットでなければ意味がありません。**
        if (!python) {
            const pythonExt = vscode.extensions.getExtension('ms-python.python');
            if (pythonExt?.isActive) {
                try {
                    const details = pythonExt.exports?.settings?.getExecutionDetails?.();
                    /** @type {string[]|undefined} */
                    const command = details?.execCommand;
                    if (command && command.length > 0 && fs.existsSync(command[0])) {
                        python = command[0];
                    }
                } catch (e) {
                    this.log.appendLine(`[backend] Python 拡張から取得できませんでした: ${e}`);
                }
            } else if (pythonExt) {
                // 待たずに温めだけ始めます。答えが要るのは start() が
                // 引き直すときだけなので、ここでは結果を見ません。
                void this.pythonExtInterpreter();
            }
        }

        // ワークスペース直下の仮想環境。
        //
        // **信頼していないフォルダでは見に行きません。** ここで拾うのは
        // そのフォルダが持ち込んだ実行ファイルで、起動すれば中身が動きます。
        // 開いただけのフォルダに含まれる python.exe を黙って実行するのは、
        // ワークスペースの信頼という仕組みが防ごうとしているものそのものです。
        if (!python && vscode.workspace.isTrusted) {
            const venvNames = ['.venv', 'venv'];
            const relative = process.platform === 'win32'
                ? path.join('Scripts', 'python.exe')
                : path.join('bin', 'python');
            venvSearch:
            for (const folder of vscode.workspace.workspaceFolders ?? []) {
                for (const name of venvNames) {
                    const candidate = path.join(folder.uri.fsPath, name, relative);
                    if (fs.existsSync(candidate)) {
                        python = candidate;
                        break venvSearch;
                    }
                }
            }
        }

        // 最後は PATH 頼み。py ランチャーは Windows で最も確実に 3.x を掴む。
        if (!python) {
            python = process.platform === 'win32' ? 'py' : 'python3';
        }

        // **この1行は消さないでください。** 「画面に何も出ない時間」の犯人が
        // ここだったので、次に遅くなったときも同じ場所で確かめられるように
        // 恒久的に残します。
        this.log.appendLine(`[backend] Python を決めました: ${python} (${Date.now() - t0} ms)`);
        return python;
    }

    /**
     * Python 拡張が選んでいるインタープリタ。無ければ空文字。
     *
     * **決して失敗しません** (reject しません)。resolvePython から `void` で
     * 呼ぶので、拒否すると未処理の Promise 拒否になります。取得できない理由は
     * すべてログへ流して空文字を返します。
     *
     * 問い合わせは1回だけで、結果は pythonExtPromise に残ります。待ち上限で
     * 取り逃がしても、次の操作では待ち時間ゼロで拾えます。
     *
     * @param {number} [waitMs] 待ち上限 (ミリ秒)。省略すると待ちません
     *   (温めるだけの呼び出し用)。
     * @returns {Promise<string>}
     */
    pythonExtInterpreter(waitMs) {
        if (!this.pythonExtPromise) {
            this.pythonExtPromise = this.askPythonExt();
        }
        if (!waitMs) {
            return this.pythonExtPromise;
        }
        return Promise.race([
            this.pythonExtPromise,
            new Promise((resolve) => {
                const timer = setTimeout(() => resolve(''), waitMs);
                // 答えが先に返ったときに、この待ち針が VSCode の終了を
                // 引き止めないようにする。
                timer.unref?.();
            }),
        ]);
    }

    /**
     * Python 拡張を activate して、選ばれているインタープリタを聞きます。
     *
     * **ここが実測 4.3 秒／18.4 秒かかる場所です。** 呼び出し側 (start()) が
     * 待つのは、1本目の Python で起動できなかったときだけです。
     *
     * @returns {Promise<string>} 実在する実行ファイルのパス。無ければ空文字。
     */
    async askPythonExt() {
        const pythonExt = vscode.extensions.getExtension('ms-python.python');
        if (!pythonExt) {
            return '';
        }
        const t0 = Date.now();
        try {
            if (!pythonExt.isActive) {
                await pythonExt.activate();
            }
            const details = pythonExt.exports?.settings?.getExecutionDetails?.();
            /** @type {string[]|undefined} */
            const command = details?.execCommand;
            if (command && command.length > 0 && fs.existsSync(command[0])) {
                this.log.appendLine(
                    `[backend] Python 拡張の答え: ${command[0]} (${Date.now() - t0} ms)`);
                return command[0];
            }
            this.log.appendLine(
                `[backend] Python 拡張はインタープリタを返しませんでした (${Date.now() - t0} ms)。`);
        } catch (e) {
            // **ここから投げ返さないこと。** この関数は void で呼ばれるので、
            // 拒否すると未処理の Promise 拒否になります。終了処理中は出力
            // チャンネルが閉じていて appendLine 自体も失敗しうるため、
            // 記録の失敗まで含めて飲み込みます (記録できない相手に
            // 記録できなかったと言う先はありません)。
            try {
                this.log.appendLine(`[backend] Python 拡張から取得できませんでした: ${e}`);
            } catch { /* 出力先がもうない */ }
        }
        return '';
    }

    /** @returns {string} */
    guiPython() {
        return vscode.workspace
            .getConfiguration('aiUsageManager')
            .get('guiPythonPath', '')
            .trim();
    }

    /**
     * @param {string} python
     * @param {string} script
     * @returns {string[]}
     */
    buildArgs(python, script) {
        // py ランチャーだけは、どの版を使うかを明示しないと 2.x を掴むことがある
        const base = path.basename(python).toLowerCase();
        const prefix = base === 'py' || base === 'py.exe' ? ['-3'] : [];
        return [...prefix, '-u', script];
    }

    // ---------------- 起動 ----------------

    /**
     * バックエンド本体の在り処。**vsix に同梱されている前提の場所です。**
     * @returns {string}
     */
    scriptPath() {
        return path.join(this.extensionPath, 'backend', 'cli.py');
    }

    /** @returns {Promise<void>} */
    async ensureStarted() {
        if (this.process) {
            return;
        }
        if (this.fatalMessage) {
            // 直せていない原因で作り直しても同じところで落ちるだけなので、
            // 分かっている理由をそのまま返す。
            throw new BackendError(this.fatalMessage, false);
        }
        if (!this.starting) {
            this.starting = this.start().finally(() => {
                this.starting = undefined;
            });
        }
        return this.starting;
    }

    /**
     * Python を決めて起動します。**外したときに一度だけ引き直します。**
     *
     * resolvePython は Python 拡張の activate を待たないので、Python 拡張だけが
     * 知っている環境 (conda、別の場所の venv、pyenv) を使っている利用者では、
     * 1本目が外れることがあります。そのときだけ Python 拡張の答えを待って、
     * もう一度だけ試します。**成功パスでは1ミリ秒も待ちません。**
     *
     * 引き直しても駄目なら、利用者に返るのは**2本目の理由**です (1本目は捨てます)。
     * 2本目は利用者が VSCode 上で選んだインタープリタなので、cli.py が返す
     * 「"<その python>" -m pip install requests」という案内が、当てずっぽうの
     * 1本目 (py / python3) に対する同じ案内よりそのまま役に立ちます。
     * fatalMessage に残るのも2本目の理由なので、次の呼び出しでの表示とも揃います。
     *
     * @returns {Promise<void>}
     */
    async start() {
        // 自分の番号を控えます。以降これが this.startGeneration と食い違ったら、
        // 「自分はもう用済み」= 引き直してはいけない、という意味です。
        const gen = ++this.startGeneration;
        const python = await this.resolvePython();
        try {
            await this.spawn(python);
            return;
        } catch (first) {
            // **自分がまだ現役のときだけ引き直します。** 起動中に kill() や
            // restart() が入っていた場合、いま受け取った拒否は「利用者が止めた」
            // 結果であって Python のせいではありません。ここを見ないと、
            // すでに別の start() が起こした子を、下の this.process?.kill() が
            // 殺しにいきます。
            if (gen !== this.startGeneration) {
                throw first;
            }
            // 設定で明示されているなら引き直さない (設定が最優先)。
            // 利用者が選んだ Python の失敗を、こちらの当てずっぽうで
            // 上書きしてはいけません。
            if (this.configuredPython()) {
                throw first;
            }
            // 同梱物が欠けているのは Python のせいではないので引き直しません。
            // ここを見ないと、壊れた vsix を掴んだ利用者が、答えの変わらない
            // 2本目のために 15 秒待たされたうえ、ログには「Python 拡張が選んで
            // いる … で試し直します」という見当違いの行が残ります。
            if (!fs.existsSync(this.scriptPath())) {
                throw first;
            }
            const fallback = await this.pythonExtInterpreter(PYTHON_EXT_FALLBACK_MS);
            // **待っている間に情勢が変わっていたら、もう起こしません。** ここは
            // 最長で 15 秒待つので、その間に dispose() される余地があります。
            // 気づかずに2本目を起こすと、拡張が居なくなったあとに Python が残ります。
            //
            // 番号も見直します。15 秒のあいだに kill() / restart() が入り、
            // 別の start() がすでに動く子を起こしていることがあるためです。
            // 入口で確かめただけでは、この待ち時間のぶんが素通りになります。
            if (this.disposed || gen !== this.startGeneration) {
                throw first;
            }
            if (!fallback || fallback === python) {
                throw first;
            }
            // 1本目が (fatal を出した直後などで) まだ生きている可能性があるので
            // 確実に始末してから引き直します。生き残っていると、その stdout が
            // 2本目の出力に混ざり、this.process も奪い合いになります。
            this.process?.kill();
            this.process = undefined;
            this.log.appendLine(
                `[backend] ${python} では起動できませんでした。` +
                `Python 拡張が選んでいる ${fallback} で試し直します。`);
            // **この1行を消さないこと。** fatalMessage を消さないと、
            // ensureStarted() の if (this.fatalMessage) が2本目の成功後も
            // 1本目の理由を投げ続けます。
            //
            // かつてここには this.stdoutBuffer = '' もありましたが、要らなく
            // なりました。読みかけの行は子ごとに持つようにしたためです
            // (spawn 参照)。インスタンスで共有していた頃は、ここで消したあとに
            // 1本目の残りがパイプから届くと、2本目の最初のフレームと癒着して
            // ready が読めなくなり、**そのまま永久に起動待ちになりました。**
            this.fatalMessage = undefined;
            await this.spawn(fallback);
        }
    }

    /**
     * 指定の Python でバックエンドを起動し、ready を受け取るまで待ちます。
     *
     * **どの Python を使うかはここでは決めません** (start() が決めます)。
     * 引き直しのために、同じ手順を別の実行ファイルで繰り返せる必要があるためです。
     *
     * @param {string} python
     * @returns {Promise<void>}
     */
    spawn(python) {
        const script = this.scriptPath();
        if (!fs.existsSync(script)) {
            return Promise.reject(new BackendError(
                t('The backend script was not found: {path}', { path: script }), false));
        }

        const args = this.buildArgs(python, script);
        this.log.appendLine(`[backend] 起動: ${python} ${args.join(' ')}`);

        // **executor を async にしないこと。** async にすると、中で投げた例外が
        // Promise ではなく未処理の拒否になり、待っている側が永久に返ってきません。
        return new Promise((resolve, reject) => {
            /** @type {cp.ChildProcessWithoutNullStreams} */
            let child;
            try {
                child = cp.spawn(python, args, {
                    // **拡張ディレクトリの中を作業ディレクトリにしないこと。**
                    // Windows では、プロセスの作業ディレクトリになっている
                    // フォルダは名前を変えられません。拡張の更新は
                    // 「フォルダを一時名へ rename してから入れ替える」ので、
                    // ここを backend/ にしていると更新もアンインストールも
                    // EPERM で失敗します (「VS Code を再起動してください」と
                    // 出るのに、再起動しても直らない状態になる)。
                    //
                    // cli.py は自分の場所を __file__ から求めるので、
                    // 作業ディレクトリには依存しません。
                    cwd: os.tmpdir(),
                    env: {
                        ...process.env,
                        // 日本語のメッセージが cp932 で化けないようにする
                        PYTHONIOENCODING: 'utf-8',
                        PYTHONUTF8: '1',
                        // ログイン画面 (PySide6) を動かす Python。空なら
                        // バックエンドと同じものを使う。表示・更新だけの
                        // Python と、PySide6 入りの Python を分けられるように
                        // するための逃げ道。
                        AI_USAGE_MANAGER_GUI_PYTHON: this.guiPython(),
                        // バックエンドの表示言語。**これが唯一の伝え方です。**
                        // メトリクスのラベル・状態の要約・エラー文は Python が
                        // 確定させて返すので (services/usage_status.py)、
                        // 拡張だけを訳しても画面の半分は元の言語のまま出ます。
                        // cli.py はログイン画面のプロセスを os.environ ごと
                        // 渡して起動するため、PySide6 のダイアログにもここから伝わります。
                        //
                        // **渡せるのは spawn するこの瞬間だけです。** Python 側は
                        // import した時点で言語を確定させます (services/i18n.py の
                        // 冒頭参照)。設定 aiUsageManager.language が変わったときに
                        // extension.js が backend.restart() を呼んでいるのは、
                        // ここを通り直さないと前の言語のままになるからです。
                        AI_USAGE_MANAGER_LANG: language(),
                    },
                });
            } catch (e) {
                reject(new BackendError(
                    t('Could not start Python ({python}): {reason}',
                        { python, reason: String(e) }),
                    false));
                return;
            }

            let settled = false;
            /** @param {string} message */
            const fail = (message) => {
                if (!settled) {
                    settled = true;
                    reject(new BackendError(message, false));
                }
            };

            child.stdout.setEncoding('utf-8');
            child.stderr.setEncoding('utf-8');

            // **読みかけの行は子ごとに持ちます。** インスタンスで共有すると、
            // 引き直しで捨てた1本目の残りが2本目のフレームに混ざります。
            // stdout の 'data' ハンドラは exit のあとも外れず (Node の 'exit' は
            // パイプの吐き出しを待たない)、start() 側で共有バッファを空にしても
            // **その後に届いた残渣には間に合いません。** 実際、1本目が改行で
            // 終わらない断片を残して死ぬと、2本目の {"event":"ready"} と癒着して
            // JSON として読めなくなり、起動待ちが永久に解決しませんでした。
            // 子ごとに持てば、捨てた側が何を書こうと自分のバッファで完結します。
            let buffer = '';
            child.stdout.on('data', (chunk) => {
                buffer += chunk;
                let index;
                while ((index = buffer.indexOf('\n')) >= 0) {
                    const line = buffer.slice(0, index).trim();
                    buffer = buffer.slice(index + 1);
                    if (!line) {
                        continue;
                    }
                    const handled = this.handleLine(line);
                    // ready を受け取って初めて「使える」とみなす。プロセスが
                    // 立ち上がっただけでは、依存不足で即死する可能性がある。
                    if (handled === 'ready' && !settled) {
                        settled = true;
                        resolve();
                    } else if (handled === 'fatal') {
                        fail(this.fatalMessage ?? t('The backend could not start.'));
                    }
                }
            });

            child.stderr.on('data', (chunk) => {
                for (const line of chunk.split(/\r?\n/)) {
                    if (line.trim()) {
                        this.log.appendLine(`[python] ${line}`);
                    }
                }
            });

            child.on('error', (e) => {
                this.log.appendLine(`[backend] 起動に失敗: ${e.message}`);
                // **自分がまだ現役のときだけ片付けます。** start() が引き直した
                // あとにこの合図が届くことがあり、そのとき this.process は
                // 2本目です。無条件に消すと、生きているプロセスへの参照を
                // 失って以後の要求が全部「動いていません」になります。
                if (this.process === child) {
                    this.process = undefined;
                }
                fail(t(
                    'Could not start Python ({python}).\n'
                    + 'Point the "aiUsageManager.pythonPath" setting at an executable.\n'
                    + '{reason}',
                    { python, reason: e.message },
                ));
            });

            child.on('exit', (code) => {
                this.log.appendLine(`[backend] 終了しました (code=${code})`);
                // ここも error と同じ理由で、現役かどうかを見てから片付けます。
                // 待っている要求を抱えているのは現役のプロセスだけなので、
                // 引き直しで捨てた1本目の死に際に巻き添えを出しません。
                const wasRunning = this.process === child;
                if (wasRunning) {
                    this.process = undefined;
                    for (const [, pending] of this.pending) {
                        pending.reject(new BackendError(
                            t('The backend exited. Run "AI-UsageManager: Show Log" '
                                + 'to see the details.'),
                            false));
                    }
                    this.pending.clear();
                }
                // **wasRunning で囲まないこと。** this.process = child は下の
                // 611 行あたりで executor の末尾に同期で入るため、起動中に死んだ
                // 場合も wasRunning は true です。かつてここは `if (!wasRunning)`
                // でしたが、そのせいで起動経路からは到達できませんでした。
                //
                // 結果として、cli.py が ready も fatal も出さずに終了したとき
                // (構文エラー、import 時の異常終了、セキュリティ製品による強制終了)
                // **この Promise は永久に解決せず**、ensureStarted の this.starting も
                // 片付かないため、以後のすべての要求が黙って止まりました。利用者から
                // 見ると「エラーも出ないまま画面が出ない」状態になります。
                //
                // settled 済みなら fail() は何もしないので、無条件に呼んで安全です
                // — 起動に成功したあとの通常の終了でも、引き直しで捨てた1本目
                // (すでに拒否済み) でも、ここは素通りします。
                fail(t('The backend exited right after starting. '
                    + 'Run "AI-UsageManager: Show Log" to see the details.'));
            });

            this.process = child;
        });
    }

    /**
     * 受け取った1行を処理し、それが起動の合図なら種類を返します。
     *
     * @param {string} line
     * @returns {'ready'|'fatal'|undefined}
     */
    handleLine(line) {
        /** @type {any} */
        let message;
        try {
            message = JSON.parse(line);
        } catch {
            this.log.appendLine(`[backend] JSON として読めない行: ${line.slice(0, 200)}`);
            return undefined;
        }

        if (message.event) {
            if (message.event === 'ready') {
                return 'ready';
            }
            if (message.event === 'fatal') {
                this.fatalMessage = String(message.data?.message ?? t('Unknown error'));
                this.log.appendLine(`[backend] 続行できません: ${this.fatalMessage}`);
                return 'fatal';
            }
            if (message.event === 'fetching' && message.data?.accountId) {
                this._onFetching.fire(String(message.data.accountId));
            }
            if (message.event === 'gui_started') {
                // pid はログにだけ残します。固まったときに、どのプロセスを
                // 見ればよいかが分かる唯一の手がかりになります。
                this.log.appendLine(
                    `[backend] ログイン画面のプロセスを起動しました (pid=${message.data?.pid})`);
                this._onGuiStarted.fire();
            }
            return undefined;
        }

        const pending = this.pending.get(message.id);
        if (!pending) {
            return undefined;
        }
        this.pending.delete(message.id);
        if (message.ok) {
            pending.resolve(message.result);
        } else {
            pending.reject(new BackendError(
                String(message.error ?? t('Unknown error')), Boolean(message.authError)));
        }
        return undefined;
    }

    // ---------------- 要求 ----------------

    /**
     * @param {string} method
     * @param {Record<string, unknown>} [params]
     * @returns {Promise<any>}
     */
    async call(method, params = {}) {
        if (this.disposed) {
            throw new BackendError(t('The extension is shutting down.'), false);
        }
        await this.ensureStarted();
        const child = this.process;
        if (!child) {
            throw new BackendError(t('The backend is not running.'), false);
        }

        const id = this.nextId++;
        return new Promise((resolve, reject) => {
            this.pending.set(id, { resolve, reject });
            child.stdin.write(JSON.stringify({ id, method, params }) + '\n', (e) => {
                if (e) {
                    this.pending.delete(id);
                    reject(new BackendError(
                        t('Could not send the request: {reason}', { reason: e.message }),
                        false));
                }
            });
        });
    }

    /**
     * @param {boolean} [reload]
     * @returns {Promise<Snapshot>}
     */
    listAccounts(reload = false) {
        return this.call('list_accounts', { reload });
    }

    /**
     * ログイン用のブラウザ画面を伴う操作。
     *
     * 利用者がダイアログを操作している間ずっと返ってきません。**時間制限を
     * 設けないでください。** 途中で諦めると、利用者がログインを終えた頃には
     * 拡張側が結果を捨てており、「操作したのに反映されない」ことになります。
     *
     * @returns {Promise<Snapshot>}
     */
    addAccount() {
        return this.call('add_account');
    }

    /**
     * 進行中のログイン画面を強制的に終了させます。
     *
     * **通知を閉じるだけでは足りません。** 子プロセスがウィンドウを一つも
     * 作らないまま固まることがあり、そのまま放置すると、バックエンド側の
     * 錠を握ったプロセスが残って以後の追加・編集・再ログインが全部
     * 「別の操作が進行中です」で弾かれます。実際に終了させるのは
     * バックエンドの仕事なので、こちらからは頼むだけです。
     *
     * 待っている addAccount() 等への応答はこれとは別に返ります (中止された
     * 旨を載せた snapshot)。**この呼び出しの完了を、あちらが返ってきた
     * 合図として扱わないでください。**
     *
     * @returns {Promise<{cancelled: boolean, message: string}>}
     */
    cancelGui() {
        return this.call('cancel_gui');
    }

    /**
     * @param {string} accountId
     * @returns {Promise<Snapshot>}
     */
    editAccount(accountId) {
        return this.call('edit_account', { accountId });
    }

    /**
     * @param {string} accountId
     * @returns {Promise<Snapshot>}
     */
    relogin(accountId) {
        return this.call('relogin', { accountId });
    }

    /**
     * @param {string} accountId
     * @returns {Promise<Snapshot>}
     */
    deleteAccount(accountId) {
        return this.call('delete_account', { accountId });
    }

    /**
     * 使用状況を1件取得します。
     *
     * usage には**判定済みの状態が入っています** (cli.py が
     * services/usage_status.py の annotate を通しています)。色や丸印を
     * こちら側で決め直さないでください。
     *
     * @param {string} accountId
     * @returns {Promise<{accountId: string, usage: import('./store').Usage}>}
     */
    fetchUsage(accountId) {
        return this.call('fetch_usage', { accountId });
    }

    /**
     * @param {string} accountId
     * @param {boolean} enabled
     * @returns {Promise<Snapshot>}
     */
    setEnabled(accountId, enabled) {
        return this.call('set_enabled', { accountId, enabled });
    }

    // ---------------- プロキシ設定 ----------------
    //
    // 拡張の設定 (aiUsageManager.proxy.*) が真であり、ここから送るものは
    // バックエンドの config.json をそれで上書きするだけです。パスワードだけは
    // 別扱い (setProxyPassword) にしてあります。settings.json に書くと平文で
    // 保存・同期されるため、拡張側では一切保持しません。

    /**
     * いまの設定を読みます。
     *
     * **パスワードそのものは返ってきません。** hasPassword の真偽だけです
     * (cli.py の get_proxy 参照)。
     *
     * @returns {Promise<{mode: string, host: string, port: number,
     *   username: string, hasPassword: boolean}>}
     */
    getProxy() {
        return this.call('get_proxy');
    }

    /**
     * プロキシ設定 (パスワードを除く) を保存します。
     *
     * @param {{mode: string, host: string, port: number, username: string}} settings
     * @returns {Promise<{saved: boolean}>}
     */
    setProxy(settings) {
        return this.call('set_proxy', settings);
    }

    /**
     * プロキシのパスワードだけを保存します。
     *
     * @param {string} password 空文字を渡すと保存済みのパスワードを消します。
     * @returns {Promise<{saved: boolean}>}
     */
    setProxyPassword(password) {
        return this.call('set_proxy_password', { password });
    }

    /**
     * いまの設定でプロキシへの接続を試します。
     *
     * message は表示用に翻訳済みの文字列が返ります (cli.py 側で
     * services/i18n.py を通して組み立てています)。ここでは訳しません。
     *
     * @returns {Promise<{reachable: boolean, message: string}>}
     */
    testProxy() {
        return this.call('test_proxy');
    }

    /**
     * 環境変数 (HTTP_PROXY / HTTPS_PROXY) からプロキシを検出します。
     * **保存はしません。** 呼び出し側が使うかどうかを決めます。
     *
     * @returns {Promise<{found: boolean, host: string, port: number}>}
     */
    detectProxy() {
        return this.call('detect_proxy');
    }
}

module.exports = { Backend, BackendError };
