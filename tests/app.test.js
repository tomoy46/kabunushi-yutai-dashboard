const test = require('node:test');
const assert = require('node:assert/strict');
const app = require('../app.js');
const base = {code:'1', name:'テスト会社', category:'金券', record_months:[3], long_term_condition:'なし', benefit_status:'official_confirmed', data_confidence:'official_confirmed', official_source_url:'https://example.com/benefit', benefit_tiers:[{shares:100, description:'商品券', annual_value_yen:3000}]};
const filters = {search:'', month:'', category:'', hundredOnly:false, longTermOnly:false, favoritesOnly:false, favorites:[], showAbolished:false, showCandidates:false};

test('優待情報を一覧用に正規化する', () => {
  const x = app.normalizeBenefitRecord(base, {market:'プライム', sector:'小売業'});
  assert.equal(x.market, 'プライム');
  assert.equal(x.industry, '小売業');
  assert.equal(x.tier.shares, 100);
  assert.equal(x.tier.description, '商品券');
  assert.equal(x.official_source_url, 'https://example.com/benefit');
  assert.equal(x.long_term_condition, 'なし');
});

test('検索と優待条件フィルター', () => {
  const x = app.normalizeBenefitRecord(base);
  assert.equal(app.filterBenefits([x], {...filters, search:'商品券', month:'3', category:'金券', hundredOnly:true}).length, 1);
  assert.equal(app.filterBenefits([x], {...filters, search:'なし'}).length, 0);
  assert.equal(app.filterBenefits([{...x, long_term_condition:'1年以上'}], {...filters, longTermOnly:true}).length, 1);
});

test('お気に入り、廃止済み、候補の表示を切り替える', () => {
  const active = app.normalizeBenefitRecord(base);
  const abolished = app.normalizeBenefitRecord({...base, code:'2', benefit_status:'abolished'});
  const candidate = app.normalizeBenefitRecord({...base, code:'3', benefit_status:'candidate'});
  assert.deepEqual(app.filterBenefits([active, abolished, candidate], filters).map(x => x.code), ['1']);
  assert.deepEqual(app.filterBenefits([active, abolished, candidate], {...filters, showAbolished:true, showCandidates:true}).map(x => x.code), ['1','2','3']);
  assert.deepEqual(app.filterBenefits([active], {...filters, favoritesOnly:true, favorites:['1']}).map(x => x.code), ['1']);
});

test('証券コード順と銘柄名順に並べる', () => {
  const a = app.normalizeBenefitRecord({...base, code:'20', name:'イ社'});
  const b = app.normalizeBenefitRecord({...base, code:'3', name:'ア社'});
  assert.deepEqual(app.sortBenefits([a,b], 'code-asc').map(x => x.code), ['3','20']);
  assert.deepEqual(app.sortBenefits([a,b], 'name-asc').map(x => x.code), ['3','20']);
});

test('新形式の極洋と複数の優待区分を保持する', () => {
  const raw = {code:'1301', name:'極洋', benefit_status:'official_confirmed', benefit_title:'自社製品', annual_value_yen:2500, official_verified_at:'2026-07-26', official_source_url:'https://www.kyokuyo.co.jp/ir/concept/', record_months:[3], long_term_required:false, benefit_tiers:[{shares:300, maximum_shares:null, description:'6,000円相当の自社製品', annual_value_yen:6000},{shares:100, maximum_shares:299, description:'2,500円相当の自社製品', annual_value_yen:2500}]};
  const x = app.normalizeBenefitRecord(raw, {market:'プライム', sector:'水産・農林業'});
  assert.equal(x.minimum_shares, 100);
  assert.equal(x.last_checked_at, '2026-07-26');
  assert.deepEqual(x.benefit_tiers.map(t => t.shares), [100,300]);
  assert.equal(x.official_source_url, 'https://www.kyokuyo.co.jp/ir/concept/');
});

test('欠損値を安全に正規化する', () => {
  const x = app.normalizeBenefitRecord({code:'9', name:null, market:'undefined', sector:null, industry:'NaN', benefit_tiers:[]});
  assert.equal(x.name, '名称未取得');
  assert.equal(x.market, '市場未取得');
  assert.equal(x.industry, '業種未取得');
  assert.equal(x.benefit_summary, '優待内容未取得');
});
