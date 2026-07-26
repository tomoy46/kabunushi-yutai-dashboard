# 株主優待ダッシュボード

日本株の株主優待について、必要株数・必要投資額・優待/配当/総合利回りを横断して比較する、静的なPWAです。現在の12銘柄と株価はすべて**デモ用サンプル**であり、投資判断には利用できません。

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
