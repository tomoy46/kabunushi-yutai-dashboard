(function (root) {
  'use strict';
  const yen = new Intl.NumberFormat('ja-JP');
  const calcInvestment = (price, shares) => price * shares;
  const calcBenefitYield = (value, investment) => Number.isFinite(value) && value >= 0 && Number.isFinite(investment) && investment > 0 ? value / investment * 100 : null;
  const calcDividendYield = (dividend, price) => Number.isFinite(dividend) && dividend >= 0 && Number.isFinite(price) && price > 0 ? dividend / price * 100 : null;
  const calcTotalYield = (benefit, dividend) => Number.isFinite(benefit) ? benefit + (Number.isFinite(dividend) ? dividend : 0) : null;
  const pct = value => Number.isFinite(value) ? `${value.toFixed(2)}%` : '算定対象外';
  const averageTotalYield = items => {
    const valid = items.filter(item => Number.isFinite(item.totalYield));
    return valid.length ? valid.reduce((sum, item) => sum + item.totalYield, 0) / valid.length : null;
  };
  const validText = (value, fallback) => value != null && !['undefined','null','NaN',''].includes(String(value).trim()) ? String(value) : fallback;
  const validNumber = value => typeof value === 'number' && Number.isFinite(value) ? value : null;
  const normalizeBenefitRecord = (benefit, listedCompany = {}) => {
    let tiers = (Array.isArray(benefit.benefit_tiers) ? benefit.benefit_tiers : []).map(tier => ({...tier,
      shares:validNumber(tier?.shares), maximum_shares:validNumber(tier?.maximum_shares),
      description:validText(tier?.description,'優待内容未取得'), annual_value_yen:validNumber(tier?.annual_value_yen)
    })).filter(tier=>tier.shares!=null).sort((a,b)=>a.shares-b.shares);
    const minimumShares=validNumber(benefit.minimum_shares)??tiers[0]?.shares??null;
    const annualValue=validNumber(benefit.annual_value_yen)??tiers.find(t=>t.shares===minimumShares)?.annual_value_yen??tiers[0]?.annual_value_yen??null;
    const summary=validText(benefit.benefit_summary,validText(benefit.benefit_title,validText(benefit.benefit_description,'優待内容未取得')));
    if(!tiers.length&&minimumShares!=null) tiers=[{shares:minimumShares,maximum_shares:validNumber(benefit.maximum_shares),description:summary,annual_value_yen:annualValue}];
    return {...benefit,code:validText(benefit.code,''),name:validText(benefit.name,'名称未取得'),
      market:validText(benefit.market,validText(listedCompany.market,'市場未取得')),
      sector:validText(benefit.sector,validText(benefit.industry,validText(listedCompany.sector,validText(listedCompany.industry,'業種未取得')))),
      industry:validText(benefit.sector,validText(benefit.industry,validText(listedCompany.sector,validText(listedCompany.industry,'業種未取得')))),
      category:validText(benefit.category,'分類未取得'),benefit_summary:summary,minimum_shares:minimumShares,
      annual_value_yen:annualValue,benefit_tiers:tiers,
      record_months:Array.isArray(benefit.record_months)?benefit.record_months.filter(Number.isFinite):[],
      annual_occurrences:validNumber(benefit.annual_occurrences),
      long_term_condition:validText(benefit.long_term_condition,benefit.long_term_required===false?'なし':'未取得'),
      last_checked_at:validText(benefit.last_checked_at,validText(benefit.official_verified_at,null))};
  };
  const isRankingEligible = benefit => benefit.benefit_status === 'official_confirmed' && benefit.data_confidence === 'official_confirmed';
  const enrich = (benefit, market = {}) => {
    const tier = benefit.benefit_tiers?.[0] || {shares:benefit.minimum_shares??null,description:benefit.benefit_summary||'優待内容未取得',annual_value_yen:benefit.annual_value_yen??null}; const price = validNumber(market.price) ?? validNumber(benefit.sample_price);
    const dividend = market.forecast_dividend ?? benefit.sample_forecast_dividend ?? null;
    const investment = price == null || tier.shares == null ? null : calcInvestment(price, tier.shares);
    const rankingEligible = isRankingEligible(benefit);
    const benefitYield = rankingEligible ? calcBenefitYield(tier.annual_value_yen, investment) : null;
    const dividendYield = calcDividendYield(dividend, price);
    const priceSample = market.price_sample ?? market.source === 'sample';
    const dividendSample = market.dividend_sample ?? market.source === 'sample';
    return {...benefit, price, dividend, price_at: market.price_at || benefit.price_at || null, priceSample, dividendSample, investment, rankingEligible, benefitYield, dividendYield, totalYield: rankingEligible ? calcTotalYield(benefitYield, dividendYield) : null, tier};
  };
  const filterBenefits = (items, filters) => items.filter(x => {
    const q = (filters.search || '').toLowerCase();
    return (!q || `${x.code} ${x.name} ${x.tier.description}`.toLowerCase().includes(q)) &&
      (!filters.month || x.record_months.includes(Number(filters.month))) && (!filters.category || x.category === filters.category) &&
      (!filters.maxInvestment || (x.investment != null && x.investment <= Number(filters.maxInvestment))) && (!filters.hundredOnly || (x.tier.shares != null && x.tier.shares <= 100)) &&
      (!filters.longTermOnly || (x.long_term_condition && x.long_term_condition !== 'なし')) &&
      (filters.showAbolished || x.benefit_status !== 'abolished') && (filters.showCandidates || x.benefit_status !== 'candidate') && (!filters.favoritesOnly || filters.favorites.includes(x.code));
  });
  const sortBenefits = (items, sort) => [...items].sort((a,b) => {
    if (sort === 'investment-asc') {
      const aValid = Number.isFinite(a.investment), bValid = Number.isFinite(b.investment);
      if (aValid !== bValid) return aValid ? -1 : 1;
      return aValid ? a.investment - b.investment : 0;
    }
    if (a.rankingEligible !== b.rankingEligible) return a.rankingEligible ? -1 : 1;
    const key = {'benefit-desc':'benefitYield','dividend-desc':'dividendYield'}[sort] || 'totalYield';
    const aValid = Number.isFinite(a[key]), bValid = Number.isFinite(b[key]);
    if (aValid !== bValid) return aValid ? -1 : 1;
    return aValid ? b[key] - a[key] : 0;
  });
  const api = {calcInvestment, calcBenefitYield, calcDividendYield, calcTotalYield, pct, averageTotalYield, normalizeBenefitRecord, isRankingEligible, enrich, filterBenefits, sortBenefits};
  if (typeof module !== 'undefined') module.exports = api;
  if (typeof document === 'undefined') return;

  let items = [], favorites = JSON.parse(localStorage.getItem('yutai-favorites') || '[]'), favoritesOnly = false;
  const $ = id => document.getElementById(id);
  const filters = () => ({search:$('search').value.trim(),month:$('month').value,category:$('category').value,maxInvestment:$('maxInvestment').value,hundredOnly:$('hundredOnly').checked,longTermOnly:$('longTermOnly').checked,showAbolished:$('showAbolished').checked,showCandidates:$('showCandidates').checked,favoritesOnly,favorites});
  const statusLabels = {official_confirmed:'公式確認済み',candidate:'公式確認未完了',abolished:'優待廃止済み'};
  const statusBadge = x => statusLabels[x.benefit_status] ? `<span class="status status-${x.benefit_status}">${statusLabels[x.benefit_status]}</span>` : '';
  const yieldText = (x, value) => !x.rankingEligible ? 'ランキング対象外' : `${pct(value)}${x.priceSample ? '（参考値）' : ''}`;
  const priceLabel = x => x.price==null?'株価 未取得':`${x.priceSample ? 'サンプル／参考株価' : '株価'} ¥${yen.format(x.price)}`;
  const updatedLabel = value => value ? `更新日時 ${value}` : '更新日時 未取得';
  const verifiedLabel = value => value ? `公式確認日 ${value}` : '公式確認日 未取得';
  const investmentLabel = x => x.price==null ? '株価未取得' : x.investment==null ? '必要投資額 未取得' : '必要投資額 ¥'+yen.format(x.investment);
  const favoriteButton = code => `<button class="favorite ${favorites.includes(code)?'on':''}" data-favorite="${code}" aria-label="お気に入り">${favorites.includes(code)?'★':'☆'}</button>`;
  function render() {
    const list=sortBenefits(filterBenefits(items,filters()),$('sort').value); $('resultCount').textContent=`${list.length}件の銘柄`; $('empty').hidden=list.length>0;
    $('tableBody').innerHTML=list.map(x=>`<tr><td class="company"><span class="code">${x.code} · ${x.market}</span><b>${x.name}</b>${statusBadge(x)}<span>${x.industry}</span></td><td><b>${priceLabel(x)}</b><span class="sub">${x.tier.shares==null?'必要株数 未取得':x.tier.shares+'株'} / ${investmentLabel(x)}<br>${updatedLabel(x.price_at)}</span></td><td class="benefit"><span class="pill">${x.category}</span><br>${x.benefit_summary}</td><td>${x.record_months.map(m=>m+'月').join('・')}</td><td>${yieldText(x,x.benefitYield)}</td><td>${x.dividendYield==null?'データなし':pct(x.dividendYield)+(x.dividendSample?'（参考値）':'')}</td><td class="yield">${yieldText(x,x.totalYield)}</td><td>${favoriteButton(x.code)}<button class="details-button" data-detail="${x.code}">詳細</button></td></tr>`).join('');
    $('cards').innerHTML=list.map(x=>`<article class="card"><div class="card-head"><div><span class="code">${x.code} · ${x.market}</span><h3>${x.name}</h3>${statusBadge(x)}<span class="sub">${x.industry} / 権利 ${x.record_months.join('・')}月</span></div>${favoriteButton(x.code)}</div><div class="card-benefit"><span class="pill">${x.category}</span> ${x.benefit_summary}<div class="sub">${x.tier.shares==null?'必要株数 未取得':x.tier.shares+'株〜'} / ${investmentLabel(x)}</div></div><div class="card-numbers"><div><b>${yieldText(x,x.benefitYield)}</b><span>優待利回り</span></div><div><b>${x.dividendYield==null?'—':pct(x.dividendYield)}</b><span>配当利回り${x.dividendSample?'（参考値）':''}</span></div><div><b class="yield">${yieldText(x,x.totalYield)}</b><span>総合利回り</span></div></div><div class="card-bottom"><span class="sub">${priceLabel(x)} / ${updatedLabel(x.price_at)}</span><button class="details-button" data-detail="${x.code}">詳細を見る →</button></div></article>`).join('');
  }
  function showDetail(code){const x=items.find(i=>i.code===code);$('detailContent').innerHTML=`<div class="detail-title"><span class="code">${x.code} · ${x.market}</span><h2>${x.name}</h2>${statusBadge(x)}<span>${x.industry} / ${verifiedLabel(x.last_checked_at)}</span></div><div class="detail-grid"><div><span>${x.priceSample?'サンプル株価':'現在株価'}</span><b>${x.price==null?'株価 未取得':'¥'+yen.format(x.price)}</b><small>${updatedLabel(x.price_at)}</small></div><div><span>${x.dividendSample?'サンプル予想年間配当':'予想年間配当'}（1株）</span><b>${x.dividend==null?'データなし':'¥'+yen.format(x.dividend)}</b></div><div><span>長期保有条件</span><b>${x.long_term_condition||'なし'}</b></div></div><p>${x.change_or_abolition_note||'変更・廃止情報なし'}</p>${x.last_record_date?`<p>最終基準日：${x.last_record_date} / 廃止日：${x.abolished_at}</p>`:''}<h3>優待区分（年${x.annual_occurrences}回）</h3><div class="tiers">${x.benefit_tiers.map(t=>`<div class="tier"><div><b>${t.shares==null?'必要株数 未取得':t.shares+'株以上'+(t.maximum_shares!=null?t.maximum_shares+1+'株未満':'')}</b><br>${t.description}</div><div>${t.annual_value_yen==null?'金額換算対象外':'年間 ¥'+yen.format(t.annual_value_yen)}</div></div>`).join('')}</div>${x.official_source_url?`<a class="official" href="${x.official_source_url}" target="_blank" rel="noopener">公式情報を確認 ↗</a>`:'<span>公式情報URL 未確認</span>'}`;$('detail').showModal()}

  document.addEventListener('click',e=>{const fav=e.target.closest('[data-favorite]');if(fav){const c=fav.dataset.favorite;favorites=favorites.includes(c)?favorites.filter(x=>x!==c):[...favorites,c];localStorage.setItem('yutai-favorites',JSON.stringify(favorites));render()}const d=e.target.closest('[data-detail]');if(d)showDetail(d.dataset.detail)});
  ['search','month','category','maxInvestment','sort','hundredOnly','longTermOnly','showAbolished','showCandidates'].forEach(id=>$(id).addEventListener(id==='search'?'input':'change',render));
  $('reset').onclick=()=>{['search','month','category','maxInvestment'].forEach(id=>$(id).value='');$('sort').value='total-desc';$('hundredOnly').checked=$('longTermOnly').checked=$('showAbolished').checked=$('showCandidates').checked=false;render()};
  $('favoritesButton').onclick=()=>{favoritesOnly=!favoritesOnly;$('favoritesButton').setAttribute('aria-pressed',favoritesOnly);render()};
  $('themeButton').onclick=()=>{const dark=document.documentElement.dataset.theme!=='dark';document.documentElement.dataset.theme=dark?'dark':'light';localStorage.setItem('yutai-theme',dark?'dark':'light')}; document.documentElement.dataset.theme=localStorage.getItem('yutai-theme')||'light';
  document.querySelector('.dialog-close').onclick=()=>$('detail').close();
  Promise.all(['benefits.json','market-data.json','verification-queue.json','listed-companies.json','discovery-progress.json'].map(name=>fetch('data/'+name).then(r=>r.json()))).then(([benefits,market,queue,master,progress])=>{const companies=Object.fromEntries(master.map(company=>[company.code,company]));items=benefits.map(b=>enrich(normalizeBenefitRecord(b,companies[b.code]),market[b.code]));[...new Set(items.flatMap(x=>x.record_months||[]))].sort((a,b)=>a-b).forEach(v=>$('month').add(new Option(v+'月',v)));[...new Set(items.map(x=>x.category).filter(Boolean))].sort().forEach(v=>$('category').add(new Option(v,v)));$('masterCount').textContent=master.length+'社';$('confirmedCount').textContent=items.filter(x=>x.benefit_status==='official_confirmed').length+'社';$('candidateCount').textContent=queue.filter(x=>x.result!=='failed').length+'社';$('abolishedCount').textContent=items.filter(x=>x.benefit_status==='abolished').length+'社';$('unresearchedCount').textContent=(master.length-new Set(progress.processed_codes||[]).size)+'社';$('failedCount').textContent=progress.failed_codes?.length+'社';const average=averageTotalYield(items);$('avgYield').textContent=average==null?'—':pct(average);$('queue').innerHTML=queue.length?queue.slice(0,50).map(x=>`<div class="queue-row"><b>${x.code||''} ${x.name||''}</b><span>${(x.record_months||[]).join('・')||'権利月 未確認'}</span><span>${x.result==='failed'?'取得失敗':'確認待ち'}</span><span>信頼度 ${x.confidence_score??'未取得'}</span><span>${(x.verification_reasons||[]).join(' / ')||'理由 未登録'}</span><span>${x.error_reason||x.evidence_text||'根拠 未取得'}</span></div>`).join(''):'<p>確認待ちはありません。</p>';render()}).catch(()=>{$('empty').hidden=false;$('empty').querySelector('b').textContent='データを読み込めませんでした';$('queue').textContent='確認待ちデータを読み込めませんでした'});
  if('serviceWorker' in navigator) navigator.serviceWorker.register('service-worker.js');
})(this);
