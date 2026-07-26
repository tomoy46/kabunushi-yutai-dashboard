(function (root) {
  'use strict';
  const yen = new Intl.NumberFormat('ja-JP');
  const calcInvestment = (price, shares) => price * shares;
  const calcBenefitYield = (value, investment) => value == null ? null : value / investment * 100;
  const calcDividendYield = (dividend, price) => dividend == null ? null : dividend / price * 100;
  const calcTotalYield = (benefit, dividend) => benefit == null ? null : benefit + (dividend || 0);
  const enrich = (benefit, market = {}) => {
    const tier = benefit.benefit_tiers[0]; const price = market.price ?? benefit.sample_price;
    const dividend = market.forecast_dividend ?? benefit.sample_forecast_dividend ?? null;
    const investment = calcInvestment(price, tier.shares);
    const benefitYield = calcBenefitYield(tier.annual_value_yen, investment);
    const dividendYield = calcDividendYield(dividend, price);
    return {...benefit, price, dividend, price_at: market.price_at || benefit.price_at, investment, benefitYield, dividendYield, totalYield: calcTotalYield(benefitYield, dividendYield), tier};
  };
  const filterBenefits = (items, filters) => items.filter(x => {
    const q = filters.search.toLowerCase();
    return (!q || `${x.code} ${x.name} ${x.tier.description}`.toLowerCase().includes(q)) &&
      (!filters.month || x.record_months.includes(Number(filters.month))) && (!filters.category || x.category === filters.category) &&
      (!filters.maxInvestment || x.investment <= Number(filters.maxInvestment)) && (!filters.hundredOnly || x.tier.shares <= 100) &&
      (!filters.longTermOnly || Boolean(x.long_term_condition)) && (!filters.favoritesOnly || filters.favorites.includes(x.code));
  });
  const sortBenefits = (items, sort) => [...items].sort((a,b) => sort === 'investment-asc' ? a.investment-b.investment : (b[{ 'benefit-desc':'benefitYield','dividend-desc':'dividendYield'}[sort] || 'totalYield'] ?? -1)-(a[{ 'benefit-desc':'benefitYield','dividend-desc':'dividendYield'}[sort] || 'totalYield'] ?? -1));
  const api = {calcInvestment, calcBenefitYield, calcDividendYield, calcTotalYield, enrich, filterBenefits, sortBenefits};
  if (typeof module !== 'undefined') module.exports = api;
  if (typeof document === 'undefined') return;

  let items = [], favorites = JSON.parse(localStorage.getItem('yutai-favorites') || '[]'), favoritesOnly = false;
  const $ = id => document.getElementById(id); const pct = n => n == null ? '算定対象外' : `${n.toFixed(2)}%`;
  const filters = () => ({search:$('search').value.trim(),month:$('month').value,category:$('category').value,maxInvestment:$('maxInvestment').value,hundredOnly:$('hundredOnly').checked,longTermOnly:$('longTermOnly').checked,favoritesOnly,favorites});
  const favoriteButton = code => `<button class="favorite ${favorites.includes(code)?'on':''}" data-favorite="${code}" aria-label="お気に入り">${favorites.includes(code)?'★':'☆'}</button>`;
  function render() {
    const list=sortBenefits(filterBenefits(items,filters()),$('sort').value); $('resultCount').textContent=`${list.length}件の銘柄`; $('empty').hidden=list.length>0;
    $('tableBody').innerHTML=list.map(x=>`<tr><td class="company"><span class="code">${x.code} · ${x.market}</span><b>${x.name}</b><span>${x.industry}</span></td><td><b>¥${yen.format(x.price)}</b><span class="sub">${x.tier.shares}株 / ¥${yen.format(x.investment)}<br>${x.price_at}</span></td><td class="benefit"><span class="pill">${x.category}</span><br>${x.tier.description}</td><td>${x.record_months.map(m=>m+'月').join('・')}</td><td>${pct(x.benefitYield)}</td><td>${x.dividendYield==null?'データなし':pct(x.dividendYield)}</td><td class="yield">${pct(x.totalYield)}</td><td>${favoriteButton(x.code)}<button class="details-button" data-detail="${x.code}">詳細</button></td></tr>`).join('');
    $('cards').innerHTML=list.map(x=>`<article class="card"><div class="card-head"><div><span class="code">${x.code} · ${x.market}</span><h3>${x.name}</h3><span class="sub">${x.industry} / 権利 ${x.record_months.join('・')}月</span></div>${favoriteButton(x.code)}</div><div class="card-benefit"><span class="pill">${x.category}</span> ${x.tier.description}<div class="sub">${x.tier.shares}株〜 / 必要投資額 ¥${yen.format(x.investment)}</div></div><div class="card-numbers"><div><b>${pct(x.benefitYield)}</b><span>優待利回り</span></div><div><b>${x.dividendYield==null?'—':pct(x.dividendYield)}</b><span>配当利回り</span></div><div><b class="yield">${pct(x.totalYield)}</b><span>総合利回り</span></div></div><div class="card-bottom"><span class="sub">株価 ¥${yen.format(x.price)} (${x.price_at})</span><button class="details-button" data-detail="${x.code}">詳細を見る →</button></div></article>`).join('');
  }
  function showDetail(code){const x=items.find(i=>i.code===code);$('detailContent').innerHTML=`<div class="detail-title"><span class="code">${x.code} · ${x.market}</span><h2>${x.name}</h2><span>${x.industry} / 情報確認日 ${x.info_checked_at}</span></div><div class="detail-grid"><div><span>現在株価</span><b>¥${yen.format(x.price)}</b><small> ${x.price_at}</small></div><div><span>予想年間配当（1株）</span><b>${x.dividend==null?'データなし':'¥'+yen.format(x.dividend)}</b></div><div><span>長期保有条件</span><b>${x.long_term_condition||'なし'}</b></div></div><h3>優待区分</h3><div class="tiers">${x.benefit_tiers.map(t=>`<div class="tier"><div><b>${t.shares}株以上</b><br>${t.description}</div><div>${t.annual_value_yen==null?'金額換算対象外':'年間 ¥'+yen.format(t.annual_value_yen)}</div></div>`).join('')}</div><a class="official" href="${x.official_url}" target="_blank" rel="noopener">公式優待ページを確認 ↗</a>`;$('detail').showModal()}
  document.addEventListener('click',e=>{const fav=e.target.closest('[data-favorite]');if(fav){const c=fav.dataset.favorite;favorites=favorites.includes(c)?favorites.filter(x=>x!==c):[...favorites,c];localStorage.setItem('yutai-favorites',JSON.stringify(favorites));render()}const d=e.target.closest('[data-detail]');if(d)showDetail(d.dataset.detail)});
  ['search','month','category','maxInvestment','sort','hundredOnly','longTermOnly'].forEach(id=>$(id).addEventListener(id==='search'?'input':'change',render));
  $('reset').onclick=()=>{['search','month','category','maxInvestment'].forEach(id=>$(id).value='');$('sort').value='total-desc';$('hundredOnly').checked=$('longTermOnly').checked=false;render()};
  $('favoritesButton').onclick=()=>{favoritesOnly=!favoritesOnly;$('favoritesButton').setAttribute('aria-pressed',favoritesOnly);render()};
  $('themeButton').onclick=()=>{const dark=document.documentElement.dataset.theme!=='dark';document.documentElement.dataset.theme=dark?'dark':'light';localStorage.setItem('yutai-theme',dark?'dark':'light')}; document.documentElement.dataset.theme=localStorage.getItem('yutai-theme')||'light';
  document.querySelector('.dialog-close').onclick=()=>$('detail').close();
  Promise.all([fetch('data/benefits.json').then(r=>r.json()),fetch('data/market-data.json').then(r=>r.json())]).then(([benefits,market])=>{items=benefits.map(b=>enrich(b,market[b.code]));[...new Set(items.flatMap(x=>x.record_months))].sort((a,b)=>a-b).forEach(v=>$('month').add(new Option(v+'月',v)));[...new Set(items.map(x=>x.category))].sort().forEach(v=>$('category').add(new Option(v,v)));$('companyCount').textContent=items.length+'社';const valid=items.filter(x=>x.totalYield!=null);$('avgYield').textContent=(valid.reduce((s,x)=>s+x.totalYield,0)/valid.length).toFixed(2)+'%';$('updatedAt').textContent=items.map(x=>x.info_checked_at).sort().at(-1);render()}).catch(()=>{$('empty').hidden=false;$('empty').querySelector('b').textContent='データを読み込めませんでした'});
  if('serviceWorker' in navigator) navigator.serviceWorker.register('service-worker.js');
})(this);
