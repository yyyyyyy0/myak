# myak

[English](README.md) | 日本語

> **myak** は日本語の **脈** に由来します。文脈、脈絡といった言葉のとおり、会話と会話をつなぐ意味の糸を、時間をまたいで残すためのツールです。

Claude Code のための永続メモリです。セッションのトランスクリプトを SQLite FTS5 にインデックス化し、過去の会話をプロジェクトやセッションをまたいで検索できるようにします。

## できること

1. **自動インデックス。** `Stop` が毎ターンの終了時にトランスクリプトを差分でインデックス化します。`SessionEnd` で最終フラッシュを行います。サブエージェントのトランスクリプトもインデックス化されます。
2. **自動リコール。** `UserPromptSubmit` がメモリを検索し、関連する文脈を `additionalContext` として注入します。
3. **プロジェクトの要約。** `SessionStart`（`startup|clear|compact`）が、現在のプロジェクトの直近のセッションを注入します。
4. **MCP ツール。** Claude Code と Claude Desktop 向けの `myak_search` と `myak_save` を提供します。
5. **手動検索。** ターミナルから `myak query` を実行できます。
6. **Obsidian へのエクスポート。** `myak export` がプロジェクトごとの Markdown ノートを書き出します。

## myak を使うべき場面と、そうでない場面

myak は検索機能付きの**生のアーカイブ**であり、要約ツールではありません。セッションで実際に交わされた内容（サブエージェントのトランスクリプトを含む）をそのまま保存し、全文検索またはベクトル検索でフックやツールから呼び出せるようにします。LLM による書き換えは一切ないため、インデックス化に API 呼び出しは発生せず、データがマシンの外に出ることもありません。

### 向いているケース

- 複数のリポジトリで作業していて、「X について何をなぜ決めたか」を、ノートを書かなくても数か月後に検索したい。
- リコールを無料かつオフラインで使いたい。API キーもクラウドアカウントも不要で、インデックス化にトークン課金も発生しない。
- セッションが長く、マルチエージェント構成である。サブエージェントのトランスクリプトもインデックス化されますが、メインスレッドが埋もれないよう順位は低めです。
- 日本語（または日英混在）で書いている。FTS インデックスは trigram ベースなので形態素解析器なしでリコールでき、オプションのベクトルモデルも多言語対応です。
- すでに `~/.claude` をマシン間で同期していて、共有のメモリデータベースを 1 つ持ちつつ、マシン固有の状態は同期から外したい。
- Obsidian でノートを管理していて、セッション履歴をそこへエクスポートしたい。

### 向いていないケース

- 生のスニペットではなく、圧縮・整理された要約がほしい。myak が注入するのは短い抜粋であり、セッションを物語として要約することはしません。
- Claude 以外のエージェントやチームとメモリを共有したい。myak は設計上ローカルかつ単一ユーザー向けです（MCP サーバーと CLI は他のツールからも使えますが、ホスト型のレイヤーはありません）。
- 何を覚えるかをエージェントに判断させたい。myak はノイズフィルタを通過したものをすべて記憶します。明示的なノートには `myak_save` がありますが、「重要な事実を自動で抽出する」ステップはありません。
- トランスクリプトに、平文でディスクに置きたくない秘密情報が含まれる。データベースは暗号化されていない SQLite です。

### 他のツールとの比較

| | myak | Claude Code auto memory (`MEMORY.md`) | claude-mem | Mem0 / Supermemory / Letta / Zep |
|---|---|---|---|---|
| 保存されるもの | ユーザー/アシスタントの生テキスト（サブエージェント含む） | Claude が書く、短く整理されたノート | ツール使用と編集を LLM で圧縮した観測記録 | 抽出された事実やグラフ。多くはホスト型 |
| 誰が決めるか | ノイズフィルタを通過したすべて | Claude が保存する価値があると判断したとき | LLM による圧縮ステップ | メモリツールを通じたエージェント、または抽出パイプライン |
| インデックス化のコスト | なし（ローカル SQLite、オプションでローカル埋め込み） | なし | セッションごとの API トークン | API トークン。ホスト型は有料クラウドプラン |
| スコープ | 1 ユーザー、複数マシン間で同期 | 1 リポジトリ、1 マシン | 1 マシン | マルチエージェント、マルチユーザー、フレームワーク非依存 |
| リコール | フックによる注入（FTS）+ CLI/MCP 経由のハイブリッド検索 | セッション開始時に読み込み（先頭 200 行） | 圧縮済み文脈のフック注入 | エージェントが呼び出す MCP ツール |
| 得意なこと | プロジェクトをまたいで「実際に何が語られたか」を探す | 好みや常設ルール | 次のセッションに向けた、セッション単位の濃い文脈 | パーソナライズ、共有/チームメモリ、時系列の事実 |

ルールや好みには `MEMORY.md` と `CLAUDE.md` を、検索できる記録には myak を使ってください。両者は役割が重なりません。[Claude Code の auto memory (MEMORY.md) との共存](#claude-code-の-auto-memory-memorymd-との共存)を参照してください。

比較内容は、各プロジェクトの公開されている説明に基づく 2026 年 10 月時点のものであり、最新ではない可能性があります。

## 必要環境

- Python 3.12 以上（[uv](https://docs.astral.sh/uv/) で管理）
- プラグインに対応した Claude Code（`claude plugin ...`）。最低限必要なバージョンはここでは確認していません。最新のリリースを使ってください。
- macOS は任意です。LaunchAgent で Obsidian へのエクスポートを毎日実行できます。

## インストール

```bash
git clone https://github.com/yyyyyyy0/myak.git
cd myak
./install.sh
```

インストーラーは次の処理を行います。

1. `uv tool install` を実行します（shim は `~/.local/bin/myak`）。
2. `myak install-hooks` を実行します（マシン固有のフックランチャー）。
3. このリポジトリをプラグインマーケットプレイスとして追加し、`myak@myak` をインストールします。
4. 任意で、従来の `settings.json` のフックを削除します。
5. 任意で、Obsidian エクスポート用の LaunchAgent をインストールします（macOS）。
6. 任意で、シェルエイリアス `claude-reload` を追加します。
7. 最初の `myak backfill` を実行します。

切り替え用の環境変数:

| Variable | Effect |
|---|---|
| `MYAK_EDITABLE=1` | 編集可能インストール。クローンはそのままの場所に置いておく必要があります。 |
| `MYAK_REMOVE_SETTINGS_HOOKS=1` | `myak migrate-settings --mode remove` を実行します。 |
| `MYAK_SHELL_ALIAS=1` | `~/.zshrc` に `claude-reload` エイリアスを追加します。 |
| `MYAK_OBSIDIAN_VAULT=<path>` | 毎日 9:17 にエクスポートする LaunchAgent をインストールします。 |

プラグインと従来の `settings.json` のフックが両方有効な場合、`UserPromptSubmit` は文脈を二重に注入します。従来のフックは削除してください。

### 手動インストール

```bash
uv tool install . --python 3.12
claude plugin marketplace add /path/to/myak
claude plugin install myak@myak
```

プラグインには、フック、MCP サーバー（`.mcp.json`）、`myak-recall` スキルが含まれます。

プラグインではなく `settings.json` のフックを使いたい場合は、`myak install-hooks` を実行してください。ランチャーを書き出し、貼り付け用のスニペットを表示します。両方を併用しないでください。

## 0.1 / 0.2 からのアップグレード

要点:

```bash
myak migrate --dry-run
myak migrate
myak migrate-settings --mode remove --dry-run
myak migrate-settings --mode remove
myak clean-legacy --dry-run
myak clean-legacy
```

古いデータベースでは、`myak migrate` を実行するまでフックは何もしません。Syncthing を使っている場合は、他のマシンがアイドル状態のときに 1 台だけでマイグレーションしてください。詳しい手順は [docs/MIGRATION.ja.md](docs/MIGRATION.ja.md) を参照してください。

## CLI リファレンス

すべてのコマンドは `myak` のサブコマンドです。

| Command | Purpose |
|---|---|
| `myak hook {stop,session-end,session-start,prompt}` | フックハンドラー。stdin から JSON を読み込みます。常に終了コード 0 で終了します。 |
| `myak index [--file F] [--session S] [--project P] [--final] [--embed]` | トランスクリプトを 1 つインデックス化します。 |
| `myak query [--limit N] [--hook] [--codex] [--hybrid\|--no-hybrid] <query>` | 検索します。`--hook` はフック用 JSON を、`--codex` はコンパクトなテキストを出力します。`myak[vec]` が利用可能な場合、ハイブリッド検索がデフォルトで有効です。 |
| `myak backfill [project] [--rebuild-fts]` | サブエージェントを含むすべてのトランスクリプトをインデックス化します。`project` はスラッグの部分一致です。 |
| `myak export [--vault V] [--since DAYS] [--project P]` | Obsidian にエクスポートします。 |
| `myak mcp` | stdio 経由の MCP サーバー。 |
| `myak migrate [--dry-run]` | スキーマをマイグレーションします。明示的に実行したときだけ動作します。 |
| `myak reindex --full` | カーソルをリセットし、backfill を再実行します。`--full` は必須です。 |
| `myak install-hooks` | ランチャーを書き出し、settings 用のスニペットを表示します。 |
| `myak migrate-settings --mode {rewrite,remove} [--dry-run] [--path P] [--launcher L]` | `settings.json` 内の myak のエントリを編集します。`.bak` を書き出します。 |
| `myak clean-legacy [--dry-run]` | uuid を持たない古い行にノイズフィルタを適用します。 |
| `myak redact-secrets [--dry-run]` | DB に保存済みの API キーやトークンを伏せ字にします（新しいセグメントは索引時に伏せ字化されます）。`bak-redact-*` バックアップには平文が残るので、確認後に削除してください。 |
| `myak embed [--limit N] [--model M] [--rebuild]` | 未処理のセグメントを埋め込みます（`myak[vec]` が必要）。`--model` は既知のモデルのみ受け付けます。既存のインデックスを別のモデルに切り替えるには `--rebuild` が必要です。 |

従来のエイリアス（`myak-index`、`myak-query`、`myak-backfill`、`myak-export`、`myak-mcp`）は、1 リリースの間だけ残されます。

### MCP サーバー

プラグインを使っている場合、サーバーは自動的に起動します。Claude Desktop では uv の shim を使います。

```json
{
  "mcpServers": {
    "myak": { "command": "~/.local/bin/myak-mcp" }
  }
}
```

クライアントが `~` を展開しない場合は、絶対パスを使ってください。ツールは `myak_search` と `myak_save` です。

## 仕組み

### ストレージ

| Path | Synced? | Content |
|---|---|---|
| `~/.claude/memory/memory.db` | yes | 同期する唯一のファイル。セッション、セグメント、FTS5（trigram）。 |
| `~/.claude/myak-state/` | no | マシン固有の状態。 |
| `  state.db` | no | 差分インデックス用のカーソル。 |
| `  hook.log` | no | フックのログ（ローテーションあり、0600）。 |
| `  bin/`, `venv-python` | no | ランチャーと、記録されたインタープリター。 |
| `  backups/` | no | マイグレーション前のバックアップ（新しい 3 件）。 |
| `  models/` | no | 埋め込みモデルのキャッシュ。 |
| `  memory.vec.db` | no | ベクトルキャッシュ。再構築可能。 |

SQLite は `journal_mode=DELETE` で動作するため、`-wal` / `-shm` ファイルは作られません。

### スキーマバージョン

`PRAGMA user_version` で管理されます。

- **v0** — 初期スキーマ。
- **v1** — segments に `uuid`、`agent_id`、`agent_type` を、sessions に `cwd` を追加し、部分ユニークインデックス `(session_id, agent_id, uuid)` を追加します。
- **v2** — SessionStart 用に `sessions(cwd, ended_at)` インデックスを追加します。

フックはマイグレーションを行いません。古いスキーマの場合は、一度だけログを残して終了コード 0 で終了します。コードは、より新しいスキーマのデータベースを開くことを拒否します。

### インデックス化

- 重複排除のキーは `uuid` です（挿入には `ON CONFLICT DO NOTHING` を使用）。uuid を持たないレコードには、ファイル名とオフセットから合成したキーを付けます。
- カーソル（パス、オフセット、サイズ、先頭 4KB のハッシュ）は、同期される DB ではなく `state.db` に保存されます。
- カーソルがない場合、フィンガープリントが変わった場合、オフセットがサイズを超えた場合、またはオフセット直前のバイトが `\n` でない場合は、ファイル全体を再スキャンします。再スキャンで重複は発生しません。
- 改行で終わっていない最終行は、次回の実行まで待ちます。`SessionEnd`、`index`、`backfill` は、パースできればその行を受け入れます。
- ノイズは除外されます。対象は、system reminder、コマンドタグ（`local-command-caveat` を含む）、メタレコード、tool-result のみのターンです。`last_assistant_message` は保存されません。
- サブエージェントのトランスクリプトは、`agent_id` と `agent_type` を付けて親セッションの配下に保存されます。

### フックのタイムアウト

`SessionEnd` のデフォルトの時間枠は 1.5 秒で、プラグインのフックからは延長できません。そのため `myak hook session-end` 自身はインデックス化を行いません。切り離したプロセスとして `index --final --embed` を起動し、200 ms 未満で終了します。settings ベースのフックで時間枠を延ばしたい場合は、`CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS` を設定してください。プラグインのフックは、このイベントについてはフックごとのタイムアウトを無視します。

`Stop` は非同期で実行されます（60 秒）。`UserPromptSubmit` は 10 秒、`SessionStart` は 5 秒です。

## リコールの品質

フックによるリコールは、強めに絞り込みます。

- `/` で始まるプロンプトはスキップします。
- 絶対スコアの下限と、相対的な下限（最良ヒットの 35%）を設けています。
- トークンが 3 つ以上のクエリでは、少なくとも 2 トークンの一致が必要です。
- 1 セッションにつき最大 1 件、全体で最大 3 件、各 220 文字までです。
- 時間減衰があります（半減期 30 日）。サブエージェントのヒットには `SUBAGENT_WEIGHT`（0.5）の重みが掛かります。
- 注入されるテキストは、エスケープされたうえで `<past-memory untrusted="true">` で囲まれます。これはデータであり、指示ではありません。

`SessionStart` は、現在のプロジェクトの直近のセッションを最大 5 件（タイトルと日付）、1500 文字を上限として一覧にします。サブエージェントの行は除外され、制御文字は取り除かれます。

## ハイブリッド検索（オプション）

FTS5 に加えてベクトル検索を行い、RRF で統合します。

myak は PyPI にはないため、extra はクローンから（または git から）インストールします。

```bash
# from your clone (replaces an existing install)
uv tool install '.[vec]' --python 3.12 --force
# or straight from GitHub, pinned to a release
uv tool install 'myak[vec] @ git+https://github.com/yyyyyyy0/myak.git@v0.3.0' --python 3.12 --force
myak embed
```

- モデル: fastembed 経由の `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`（384 次元）。
- ダウンロードは約 0.22 GB で、`myak embed` を実行したときだけ行われます。フックが何かをダウンロードすることはありません。
- ベクトルは `memory.vec.db` に保存されます。これは派生キャッシュで、ローカルのみ・同期なし・再構築可能です。埋め込みに使ったモデルが検索でも使われ続けます。書き込みと再構築を行うのは `myak embed` だけです（検索やフックは行いません）。
- `models/<model>.ready` は、モデルを一度読み込めた後に書き出されます。フックと検索は、このファイルがある場合にだけモデルを利用可能とみなします（fastembed 自身のキャッシュディレクトリ名はモデル ID と一致しないためです）。
- コサイン距離が 0.6（`VEC_MAX_DISTANCE`）を超えるハイブリッドのヒットは除外されます。ベクトルのみのヒットは、キーワード一致のガードを通りません。
- `myak-state/` の `embed.lock` が埋め込みを直列化します。2 つ目の実行は "another embed is running" と表示します。`SessionEnd` は 1 回の実行につき最大 500 セグメントを、nice を付けて埋め込みます。
- `UserPromptSubmit` フックは FTS のみのままです。ハイブリッド検索は `myak query`、MCP、backfill に適用されます。`SessionEnd` はインデックス化の後に埋め込みを行います。
- extra がない場合やモデルがない場合、検索は FTS のみにフォールバックします。
- キルスイッチ: `MYAK_DISABLE_VEC` は `1`、`true`、`yes`、`on`（大文字小文字は区別しません）で有効になります。それ以外の値ではベクトル検索は有効のままです。

## マシン間の同期

同期するのは `~/.claude/memory/`（`memory.db` のみ）だけです。`~/.claude/myak-state/` は絶対に同期しないでください。

[Syncthing](https://syncthing.net/) を使います。各マシンで `.stignore.example` を `~/.claude/memory/.stignore` にコピーしてください。`.stignore` はデバイスごとの設定です。`*-journal`、`*-wal`、`*-shm` を無視します。

注意点:

- DB はファイル全体として同期されます。2 台で同時に書き込むと、`*.sync-conflict-*` というコピーができることがあります。むやみに削除しないでください。もう一方のマシンだけがインデックス化したセッションが含まれている可能性があります。
- ターンごとのインデックス化は、従来のセッションごとのインデックス化よりも DB への書き込み頻度が高いため、競合が起きやすくなります。2 台を同時に動かすのは避けてください。
- マシンごとの DB や uuid によるマージは対象外です。
- マイグレーションは 1 台だけで行ってください（[docs/MIGRATION.ja.md](docs/MIGRATION.ja.md) を参照）。

## Claude Code の auto memory (MEMORY.md) との共存

両者は役割が異なります。

| | MEMORY.md (auto memory) | myak |
|---|---|---|
| Content | 整理された小さなノート | 過去の会話の大規模なアーカイブ |
| Loading | セッション開始時に常に読み込まれる | オンデマンド: フックによる注入、MCP 検索 |
| Best for | 変わらない好みやルール | 「何を決めたか / X をどう直したか」 |

指針:

- 安定したルールや好みは MEMORY.md に置いてください。myak にコピーしたり、チャット履歴を MEMORY.md にミラーしたりしないでください。
- 役に立つものの、MEMORY.md に入れるには細かすぎる、または大きすぎる事実には `myak_save` を使ってください。
- トークンコスト: MEMORY.md は毎セッション消費します。myak がトークンを消費するのは、フックが注入するとき（1 プロンプトあたり短いヒット最大 3 件、セッション開始時は最大 1500 文字）か、ツールが呼ばれたときだけです。
- 同じ事実が両方にある場合は、常に読み込まれる側が優先されます。重複は、myak 側に保存しないことで避けてください。

## 設定

| Variable | Default | Description |
|---|---|---|
| `CLAUDE_DIR` | `~/.claude` | Claude Code の設定ディレクトリ |
| `MYAK_DIR` | `$CLAUDE_DIR/memory` | 同期される DB のディレクトリ |
| `MYAK_STATE_DIR` | `$CLAUDE_DIR/myak-state` | マシン固有の状態 |
| `MYAK_OBSIDIAN_VAULT` | (none) | エクスポート先のデフォルト vault。`install.sh` では LaunchAgent が有効になります |
| `MYAK_BIN` | (none) | フックランチャーが使う `myak` バイナリを明示的に指定します |
| `MYAK_DISABLE_VEC` | (unset) | `1`/`true`/`yes`/`on` でベクトル検索を無効にします |
| `CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS` | Claude Code default (1500) | Claude Code の SessionEnd の時間枠。myak は読み取りません。 |

ランチャーは、次の順でバイナリを探します: `$MYAK_BIN`、`~/.local/bin/myak`、記録された venv の python、PATH 上の `myak`。`uv tool run` は使いません。

## アンインストール

```bash
./uninstall.sh
```

settings のフック、プラグインとマーケットプレイス、LaunchAgent、従来のランチャー、uv tool を削除します。データは削除されません。`~/.claude/memory/` と `~/.claude/myak-state/` は残ります。

## ライセンス

MIT
