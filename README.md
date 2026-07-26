# 株主優待ダッシュボード

## 株主優待の自動調査（OpenAI版）

現在の自動調査には、Gemini版ではなく **OpenAI Responses API版** を使用します。GitHub Actions の
「Discover shareholder benefits with OpenAI」は手動実行専用です。OpenAI APIの利用にはChatGPTの契約とは
別に支払い設定とAPIキーが必要で、GitHub Secret `OPENAI_API_KEY` に保存します。初期モデルは
`gpt-5.4-nano` で、必要な場合だけRepository Variable `OPENAI_MODEL` で変更できます。Web検索にはモデル料金とは
別の料金が発生します。

最初は `diagnostic_mode` で極洋（1301）1社だけを実行してください。通常実行では `security_codes` に
`7550,9861,8163` のようなカンマ区切りのコードを指定できます。証券コード範囲による全社走査は行いません。
ChatGPTの契約情報やAPIキーはログ、JSON、Issue、コミットなどで公開しないでください。Gemini Workflowは履歴と
手動での比較確認のために残していますが、現在はOpenAI版を使用し、自動スケジュールでは実行しません。

OpenAI版は検索付きリクエストに `max_tool_calls=1` を指定します。Responses APIの `output` に同じ検索呼び出しを
表す複数の `web_search_call` 項目が含まれる場合でも、HTTP 200と構造化出力が得られれば処理を継続します。
`data/openai-api-usage.json` では、検索ツールを設定したリクエスト数（`responses_with_web_search`）と返却された
項目数（`web_search_output_items`）を分けて記録します。費用の確認には、output項目数の推測ではなくOpenAIの
Usage画面と `responses_with_web_search` を使用してください。診断モードはこれらの件数と安全なaction種別だけを
表示し、検索語、URL、検索結果本文は表示せず、データファイルも更新しません。

日本株の株主優待について、優待内容・必要株数・権利月・長期保有条件・公式確認状態を比較する静的なPWAです。既存の株主優待データ13社（極洋を含む）を収録しています。株価情報は表示せず、投資判断には利用できません。

## 機能

- 証券コード・銘柄名・優待内容の検索、権利月・カテゴリー・100株・長期条件の絞り込み
- 証券コード/銘柄名の並べ替え、端末内 `localStorage` のお気に入り、ライト/ダークテーマ
- PCの表、スマートフォンのカード、各社の全 `benefit_tiers` を示す詳細ダイアログ
- インストール可能なmanifest、Service Workerによるオフラインキャッシュ
- 優待区分ごとの必要株数と内容、権利月、長期保有条件、公式URLの表示
- 制度変更・廃止・公式確認状況の表示、廃止済み銘柄フィルター

## ローカル実行とテスト

Node.js 18以上、Python 3.10以上を使用します。依存パッケージはありません。

```bash
npm test
npm run serve
# http://localhost:8000
```

## データ構成と更新

| ファイル | 用途 |
|---|---|
| `data/benefits.csv` / `.json` | 手動確認済みの優待マスター |
| `data/update-status.json` | 更新処理の結果 |
| `data/review-queue.json` | TDnetから検出した人手確認待ち候補 |
| `data/research-log.json` | 公式確認できなかった調査結果（ダッシュボード件数の対象外） |

```bash
python scripts/csv_to_json.py
python scripts/fetch_tdnet.py --feed-url 'TDnetのRSS/XML URL'
```

TDnet処理はタイトルを指定キーワードで抽出し、URL重複を除いてレビューキューに追加するだけで、優待マスターを変更しません。優待データ更新の定期処理は平日09:15 JST（00:15 UTC）です。

### 証券コードから調査対象を追加する

Actions の **Discover shareholder benefits with OpenAI** を手動実行し、`security_codes` に調査するコードだけを
カンマ区切りで入力します。個別指定のほか、`data/benefit-universe.csv` の候補と、タイトルまたは `code` 項目から
証券コードを取得できるTDnet優待開示だけが対象です。

```bash
python scripts/discover_benefits_with_openai.py --security-codes 7550,9861,8163
```

コード範囲は受け付けません。A付きコードは明示指定または候補根拠がある場合に限って対象になります。
`batch_size`（1回の調査社数）の初期値は5社で、必要に応じて実行画面で変更できます。手動指定は入力順で選ばれ、
公式情報を確認できた結果だけが `data/benefits.csv` と `data/benefits.json` に追加されます。
確認できなかった結果やAPI取得失敗は `data/research-log.json` に保存され、通常一覧と件数には加わりません。

## 公開構成

静的ファイルだけで公開できます。Cloudflare Pagesで公開する場合もWorkers KVやPages Functionsのbindingは不要です。GitHub Actionsの優待データ更新処理にも外部の株価APIやCloudflareの認証情報は使用しません。

## 制限・未実装

- TDnetはフィードURLを固定していません。提供形式に応じた運用設定が必要で、PDF本文解析や優待マスターへの自動反映は意図的に行いません。
- SVGアイコンのみです。一部PWAストア向けにはPNGアイコン追加が必要な場合があります。
- 静的アプリのため、お気に入り・テーマはブラウザ間で同期されません。オフライン時は最後に正常取得したキャッシュを表示します。

## 優待候補台帳と公式確認フロー

`data/benefit-universe.csv` は大量登録用の候補台帳です。候補は、企業公式IRで制度の実施と条件を確認するまで `candidate`（公式確認未完了）のままとし、利回り計算・ランキングから除外します。空欄は推測で補完しません。

```bash
python scripts/merge_benefit_universe.py
```

このコマンドは既存コードを上書きせず `data/benefits.json` に新規候補だけを統合し、当月・翌月・翌々月、変更開示、その他の順で `data/verification-queue.json` を再生成します。公式ページで確認したレコードだけを `official_confirmed` に昇格し、確認URLと日付を保存してください。廃止時は `abolished` と最終基準日を保持します。

## Geminiによる全上場会社の自動調査（旧方式）

この節は履歴・手動比較用に残した旧Gemini版の説明です。通常の調査には上記OpenAI版を使用してください。`data/listed-companies.json` を実在する上場会社マスターとして読み込み、`scripts/discover_benefits_with_gemini.py` が Gemini Structured Outputs と Google Search grounding で調査します。

### APIキーと手動実行

1. Google AI StudioでGemini APIキーを作成します。
2. GitHubの **Settings → Secrets and variables → Actions → New repository secret** で、名前を `GEMINI_API_KEY`、値をAPIキーとして登録します。キーはコード、データ、ログには保存されません。
3. Actionsの **Discover shareholder benefits with Gemini → Run workflow** から件数・コード範囲・失敗再試行などを指定します。ローカルでは次のように実行できます。

```bash
GEMINI_API_KEY='...' python scripts/discover_benefits_with_gemini.py --batch-size 10
# 失敗分だけ再試行し、コード範囲も限定
GEMINI_API_KEY='...' python scripts/discover_benefits_with_gemini.py --batch-size 10 --start-code 2000 --end-code 3999 --retry-failed
# データを登録せず、モデル一覧→通常呼び出し→検索→構造化抽出を診断
GEMINI_API_KEY='...' python scripts/discover_benefits_with_gemini.py --diagnostic-mode
```

起動時に Gemini API の `models.list` を取得し、`generateContent` 対応モデルだけを候補にします。ただし一覧の表示だけでは採用せず、候補順に通常呼び出し、Google Search grounding、JSON Schemaによる構造化出力を実際に検査し、各機能に成功したモデルを選びます。候補順は検索・抽出とも `gemini-3.1-flash-lite`、`gemini-3.5-flash-lite`、`gemini-3.6-flash`、`gemini-3.5-flash`、`gemini-flash-latest`、`gemini-2.5-flash-lite`、`gemini-2.5-flash` です。成功結果はAPIキーを含めず `data/gemini-model-status.json` に保存し、24時間再利用します。APIキー、リクエストURL、モデル一覧レスポンスはログへ出しません。

通常は1社につきGoogle検索付きリクエスト1回と構造化抽出1回です。Google検索は429でも再送せず、銘柄を失敗扱いにせず処理位置を維持して停止します。429のログにはQuotaFailureの割り当て項目と再試行時間だけを安全に記録します。全API呼び出しの間隔は最低1秒で、初期日次上限は100社です。`daily_limit` は同日の `data/api-usage.json` を参照して上限を適用します。成功・確認待ち・失敗・所要時間も同ファイルへ記録します。

各社の結果後に優待データ、確認待ちキュー、`data/discovery-progress.json` をアトミック更新するため、途中終了しても次の会社から再開できます。90日以内に公式確認した既存データ、および既存の公式確認済み・廃止済みレコードは上書きしません。取得可能で公式ドメインと検証できたURLだけを採用し、確定条件不足、矛盾、変更予定、PDF失敗などは確認待ちへ送ります。

### 現在の制限

- このリポジトリには全上場会社マスターを同梱せず、既存の実在13社を試験入力にしています。全社走査には同形式の信頼できる実在会社マスターの投入が必要です。
- 入力済み会社のGemini調査、公式URL検証、再開、キュー表示、利用量記録が動作します。
- TDnetのキーワード検出は既存の `fetch_tdnet.py` が確認キューを作るところまでです。TDnet API/フィードの恒久的な取得先や、検出銘柄を自動的に最優先する統合は未実装です。
- Google Search groundingや企業サイト側のアクセス制限により、取得不能・PDF解析不能になる場合は人手確認が必要です。APIキーがない開発環境では実API試験は実施できません。
