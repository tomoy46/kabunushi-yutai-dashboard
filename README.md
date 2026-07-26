# 株主優待ダッシュボード

日本株の株主優待について、必要株数・必要投資額・優待/配当/総合利回りを横断して比較する、静的なPWAです。優待情報は企業公式情報で確認済みの10社と廃止済み2社を収録しています。株価・配当はJ-Quants未設定のため参考用サンプルであり、投資判断には利用できません。

## 機能

- 証券コード・銘柄名・優待内容の検索、権利月・カテゴリー・投資額・100株・長期条件の絞り込み
- 利回り/投資額の並べ替え、端末内 `localStorage` のお気に入り、ライト/ダークテーマ
- PCの表、スマートフォンのカード、各社の全 `benefit_tiers` を示す詳細ダイアログ
- インストール可能なmanifest、Service Workerによるオフラインキャッシュ
- 金額換算できない割引券は金額を推定せず「算定対象外」、配当予想なしは「データなし」
- 制度変更・廃止・公式確認状況の表示、廃止済み銘柄フィルター（廃止済み・未確認は利回りランキング対象外）

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
| `data/market-data.json` | 株価、予想年間配当、取得日時、データ源、サンプル判定 |
| `data/update-status.json` | 更新処理の結果 |
| `data/review-queue.json` | TDnetから検出した人手確認待ち候補 |

```bash
python scripts/csv_to_json.py
python scripts/update_market_data.py
python scripts/fetch_tdnet.py --feed-url 'TDnetのRSS/XML URL'
```

`market_data.py` はproviderを分離しています。現在はAPIキー不要の `SampleProvider` のみで、将来 `JQuantsProvider` を実装して切り替えられます。取得失敗時は当該銘柄の前回値を保持します。TDnet処理はタイトルを指定キーワードで抽出し、URL重複を除いてレビューキューに追加するだけで、優待マスターを変更しません。定期処理は平日09:15 JST（00:15 UTC）です。

## GitHub Pages公開

1. PRを `main` にマージします。
2. GitHubの **Settings → Pages → Build and deployment → Source** で **GitHub Actions** を選択します。
3. `Deploy GitHub Pages` workflowの完了後、表示されたPages URLへアクセスします。以後 `main` へのpushでテスト後に自動公開されます。
4. 対応ブラウザでページを開き、「ホーム画面に追加」または「アプリをインストール」を選びます。

## 制限・未実装

- J-Quants連携、認証/APIキー管理、実データの取得は未実装です。現在の更新workflowはサンプル値を再処理します。
- TDnetはフィードURLを固定していません。提供形式に応じた運用設定が必要で、PDF本文解析や優待マスターへの自動反映は意図的に行いません。
- SVGアイコンのみです。一部PWAストア向けにはPNGアイコン追加が必要な場合があります。
- 静的アプリのため、お気に入り・テーマはブラウザ間で同期されません。オフライン時は最後に正常取得したキャッシュを表示します。
- 株価と予想配当は引き続きサンプル値で、画面上でも「サンプル」「参考値」と明記します。

## 計算

- 必要投資額 = 株価 × 必要株数
- 優待利回り = 年間優待価値 ÷ 必要投資額 × 100
- 配当利回り = 1株当たり予想年間配当 ÷ 株価 × 100
- 総合利回り = 優待利回り + 配当利回り

## 優待候補台帳と公式確認フロー

`data/benefit-universe.csv` は大量登録用の候補台帳です。候補は、企業公式IRで制度の実施と条件を確認するまで `candidate`（公式確認未完了）のままとし、利回り計算・ランキングから除外します。空欄は推測で補完しません。

```bash
python scripts/merge_benefit_universe.py
```

このコマンドは既存コードを上書きせず `data/benefits.json` に新規候補だけを統合し、当月・翌月・翌々月、変更開示、その他の順で `data/verification-queue.json` を再生成します。公式ページで確認したレコードだけを `official_confirmed` に昇格し、確認URLと日付を保存してください。廃止時は `abolished` と最終基準日を保持します。

## Geminiによる全上場会社の自動調査

`data/listed-companies.json` を実在する上場会社マスターとして読み込み、`scripts/discover_benefits_with_gemini.py` が Gemini Structured Outputs と Google Search grounding で100社ずつ調査します。現在は安全な試験入力として既存12社のみを収録しています。J-Quants契約後は同じ `code`、`name`、`market`、`sector`（可能なら `official_domain` も）のJSON配列を上場銘柄一覧から生成して差し替えられます。証券コードの連番や会社名の生成は行いません。

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

- このリポジトリには全上場会社マスターを同梱せず、既存の実在12社を試験入力にしています。全社走査にはJ-Quantsの上場銘柄一覧、または同形式の信頼できる実在会社マスターの投入が必要です。
- J-Quants未設定でも、入力済み会社のGemini調査、公式URL検証、再開、キュー表示、利用量記録は動作します。株価・配当の実データ更新は行いません。
- TDnetのキーワード検出は既存の `fetch_tdnet.py` が確認キューを作るところまでです。TDnet API/フィードの恒久的な取得先や、検出銘柄を自動的に最優先する統合は未実装です。
- Google Search groundingや企業サイト側のアクセス制限により、取得不能・PDF解析不能になる場合は人手確認が必要です。APIキーがない開発環境では実API試験は実施できません。
