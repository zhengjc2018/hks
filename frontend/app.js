// ============================================================
// A股机会雷达 · 前端主逻辑 v5（完全重建版）
// 基于 app_preview_v5.html 结构，接入后端 API
// ============================================================

const APP_VERSION = (function(){
  try { return new URL(document.currentScript.src).searchParams.get('v') || 'unknown'; }
  catch (e) { return 'unknown'; }
})();

/* ---------- 状态 ---------- */
let curDark = false;
let overlayOn = true;
let sectorCache = [];
let currentRegime = '';
let paused = false;
let refreshTimer = null;
let loadingCount = 0;   // 必须在 loadAll() 调用前声明（防 TDZ 崩溃）
let _tScanning = false; // 做T扫描防重入（一轮扫描可能持续数秒）
let _knownReadyStocks = new Set();
let _knownTSignals = new Set();
let _readyNotifiedBaseline = false;
let _tsignalNotifiedBaseline = false;
// regime 蓝框内摘要（覆盖板块数 / 战略关注 / 观察池支数）
let _sectorTotal = 0, _overlayFavor = 0, _watchTotal = 0;
// 持仓星标缓存：key=code|market，value={code,market,name}
let _holdings = new Map();
// 板块关注持久化缓存（含当前行情列表暂缺的板块，避免切换时误删）
let savedWatched = new Set();
function updateRegimeSummary(){
  const el = $('regimeSummary');
  if(!el) return;
  const parts = [];
  if(_sectorTotal) parts.push('覆盖 <b>'+_sectorTotal+'</b> 个板块');
  if(_overlayFavor) parts.push('战略关注 <b>'+_overlayFavor+'</b>');
  if(_watchTotal) parts.push('观察池 <b>'+_watchTotal+'</b> 支');
  el.innerHTML = parts.join(' · ');
}

/* ---------- 交易时段判断 ---------- */
// A股交易时段：周一至周五 09:30-11:30、13:00-15:00（午休与休市均不自动刷新）
function isTradingTime(){
  const now = new Date();
  const day = now.getDay();            // 0=周日 6=周六
  if(day === 0 || day === 6) return false;
  const t = now.getHours()*60 + now.getMinutes();
  const morning   = 9*60+30, morningEnd = 11*60+30;
  const afternoon = 13*60,   afternoonEnd = 15*60;
  return (t >= morning && t <= morningEnd) || (t >= afternoon && t <= afternoonEnd);
}
// 当前交易档位：上午=M 下午=A 非交易=null（用于判断 LLM 板块结论是否需重拉）
function currentTradeSlot(){
  const now = new Date();
  const t = now.getHours()*60 + now.getMinutes();
  if(t >= 9*60+30 && t <= 11*60+30) return 'M';
  if(t >= 13*60   && t <= 15*60)   return 'A';
  return null;
}
// 板块首屏加载标记 + 上次评述档位（避免每次刷新都重拉 LLM 板块结论）
let _sectorsFirstLoaded = false;
let _lastCommentarySlot = null;

/* ---------- 工具 ---------- */
function $(id){ return document.getElementById(id); }
function api(url, opts){
  const t = (opts && opts.timeout) || 25000;   // 默认 25s 超时，防止单个接口挂死整页
  const ctrl = new AbortController();
  const tid = setTimeout(()=>ctrl.abort(), t);
  return fetch(url, Object.assign({credentials:'same-origin', signal: ctrl.signal}, opts||{}))
    .then(r=>{
      if(!r.ok) throw new Error('HTTP '+r.status);
      return r.json();
    })
    .finally(()=>clearTimeout(tid));
}
function esc(s){ const d=document.createElement('div'); d.textContent=s; return d.innerHTML; }
function fmtPct(v){ if(v==null) return '-'; const s=Number(v).toFixed(2); return v>=0?'+'+s:s; }
function fmtPrice(v){ if(v==null || v==='' || isNaN(Number(v))) return '--'; const n=Math.abs(Number(v)); const s=n.toFixed(1); const a=s.split('.'); a[0]=a[0].replace(/\B(?=(\d{3})+(?!\d))/g,','); return (Number(v)<0?'-':'')+a.join('.'); }
function cls(v,c){ return v>=0?c+' up':c+' down'; }

/* ---------- 持仓星标 ---------- */
function _holdKey(code, market){ return String(code)+'|'+String(market); }
function _parseSecid(secid){
  const p = (secid||'').split('.');
  if(p.length === 2 && /^\d+$/.test(p[0]) && /^\d{6}$/.test(p[1])){
    return {market: parseInt(p[0],10), code: p[1]};
  }
  const code = String(secid||'').replace(/\D/g,'');
  if(/^\d{6}$/.test(code)) return {market: /^[689]/.test(code)?1:0, code};
  return {market:1, code:''};
}
function _isHolding(code, market){ return _holdings.has(_holdKey(code, market)); }
async function loadHoldings(){
  try{
    const d = await api('/api/holdings');
    _holdings = new Map((d.holdings||[]).map(h => [_holdKey(h.code, h.market), {code: h.code, market: h.market, name: h.name||''}]));
  }catch(e){ console.warn('持仓标记加载失败:', e); }
}
async function loadWatchedBoards(){
  try{
    const d = await api('/api/watched_boards');
    savedWatched = new Set([...savedWatched, ...(d.bks||[]).map(String)]);
    updateWatchHint();
  }catch(e){ console.warn('关注板块加载失败:', e); }
}
async function toggleHolding(secid, name, ev){
  if(ev) ev.stopPropagation();
  const {code, market} = _parseSecid(secid);
  if(!/^\d{6}$/.test(code)){ alert('无法识别股票代码'); return; }
  const key = _holdKey(code, market);
  const adding = !_holdings.has(key);
  try{
    await api('/api/holdings', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({action: adding?'add':'remove', code, market, name: name||''})});
    if(adding) _holdings.set(key, {code, market, name: name||''});
    else _holdings.delete(key);
    _refreshHoldingUI();
    toast(adding ? '已加入持仓' : '已取消持仓');
  }catch(e){ alert('操作失败：'+e.message); }
}
function _refreshHoldingUI(){
  document.querySelectorAll('[data-holding-star]').forEach(el => {
    const code = el.dataset.code, market = parseInt(el.dataset.market,10);
    const on = _isHolding(code, market);
    el.textContent = on ? '★' : '☆';
    el.title = on ? '已加入持仓，点击移除' : '点击加入持仓';
    el.classList.toggle('on', on);
    const card = el.closest('.lc-card');
    if(card) card.classList.toggle('holding', on);
  });
}

/* ---------- 初始化：读持久化设置 ---------- */
(function init(){
  // 夜间模式
  try{ curDark = localStorage.getItem('apanel_dark')==='1'; }catch(e){}
  if(curDark) document.body.dataset.theme='dark';
  syncThemeBtn();

  // 叠加层开关（仅保留顶部按钮开关，设置面板已移除参数配置）
  try{
    const ov = JSON.parse(localStorage.getItem('apanel_overlay')||'{}');
    if(typeof ov.on==='boolean') overlayOn = ov.on;
  }catch(e){}
  syncOvUI();

  // 设置面板筛选值
  try{
    const f = JSON.parse(localStorage.getItem('apanel_filter')||'{}');
    if(f.priceMin!=null) $('filtPriceMin').value=f.priceMin;
    if(f.priceMax!=null) $('filtPriceMax').value=f.priceMax;
    if(f.main!=null) $('filtMain').checked=f.main;
    if(f.chiNext!=null) $('filtChiNext').checked=f.chiNext;
    if(f.st!=null) $('filtSt').checked=f.st;
    if(f.mcap) $('filtMcap').value=f.mcap;
  }catch(e){}

  // 绑定事件
  $('themeBtn').onclick = toggleTheme;
  $('btnPause').onclick = togglePause;
  $('btnSearchStock').onclick = doSearchStock;
  $('stockSearchInput').addEventListener('keydown', e=>{ if(e.key==='Enter') doSearchStock(); });
  $('ovBtn').onclick = function(){ toggleOverlay(this); };

  // 启动数据拉取
  loadHoldings();
  loadWatchedBoards();
  loadAll();
  setTimeout(scanTTrade, 1200);

  // 自动刷新
  startAutoRefresh();
})();

/* ---------- 夜间模式 ---------- */
function toggleTheme(){
  curDark = !curDark;
  document.body.dataset.theme = curDark ? 'dark' : '';
  try{ localStorage.setItem('apanel_dark', curDark?'1':'0'); }catch(e){}
  syncThemeBtn();
}
function syncThemeBtn(){
  $('themeBtn').textContent = curDark ? '☀ 日间' : '🌙 夜间';
}

/* ---------- 自动刷新 ---------- */
function startAutoRefresh(){
  clearInterval(refreshTimer);
  const sec = parseInt($('refreshInterval').value)||15;
  if(!paused){
    refreshTimer = setInterval(()=>{
      if(!isTradingTime()){
        $('updated').textContent = '非交易时段 · 已暂停自动刷新';
        return;   // 休市/午休不拉数据，避免无谓刷新
      }
      loadAll();
      scanTTrade();
    }, sec*1000);
  }
}
$('refreshInterval').onchange = startAutoRefresh;
function togglePause(){
  paused = !paused;
  $('btnPause').textContent = paused ? '▶ 继续' : '暂停';
  startAutoRefresh();
}

/* ---------- 全量数据加载 ---------- */
function loadAll(){
  if(loadingCount>0) return; // 防重叠
  loadingCount++;
  Promise.allSettled([
    loadMarket(),
    loadSectors(),
    loadLifecycle()
  ]).finally(()=>{
    loadingCount--;
    $('updated').textContent = new Date().toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'})+' 更新';
  });
}

/* ---------- 区1：大盘定调 ---------- */
async function loadMarket(){
  try {
    const d = await api('/api/market');
    renderMarket(d);
  } catch(e) {
    console.warn('大盘数据加载失败:', e);
    $('marketSub').textContent = '（暂无数据）';
  }
}
function renderMarket(d){
  // 姿态标签
  const pt = $('postureTag');
  pt.textContent = d.posture || '--';
  pt.className = 'posture' + (d.posture_strong ? ' strong' : '');

  // 副标题
  $('marketSub').textContent = d.sub || '';

  // 四指数
  const idxs = d.indices || [];
  if(idxs.length){
    $('idxRow').innerHTML = idxs.map(i => `
      <div class="idx">
        <div class="nm">${esc(i.name)}</div>
        <div class="pr ${cls(i.pct,'up')}">${fmtPrice(i.price)}</div>
        <div class="pc ${cls(i.pct,'up')}">${fmtPct(i.pct)}%</div>
      </div>`).join('');
  }

  // 涨跌家数
  const b = d.breadth;
  if(b){
    $('breadth').innerHTML =
      `<span>涨跌家数</span> &nbsp; <b class="up">${b.up||0}</b> 涨 / <b class="down">${b.down||0}</b> 跌` +
      (b.tag ? ` &nbsp; <span class="tag ${b.up>b.down?'up':'down'}">${esc(b.tag)}</span>` : '');
  }

  // 走势评述（后端返回 commentary 对象：{summary, llm_summary, indices_detail}）
  const mc = $('mktComm');
  const comm = d.commentary || {};
  const mcText = comm.summary || comm.llm_summary || '';
  if(mcText){
    $('mktCommText').textContent = mcText;
    mc.style.display='';
  } else {
    mc.style.display='none';
  }
}

/* ---------- 区2：板块矩阵 ---------- */
async function loadSectors(){
  try {
    const d = await api('/api/sectors_lite', {timeout: 90000});  // 轻量列表；冷启动 TDX 连接可能数十秒，放宽超时避免误报失败
    sectorCache = d.sectors || [];
    const freshWatched = new Set(sectorCache.filter(s=>s.watched).map(s=>String(s.bk)));
    savedWatched = new Set([...savedWatched, ...freshWatched]);
    currentRegime = d.regime || '';
    const slot = currentTradeSlot();
    if(!_sectorsFirstLoaded){
      _sectorsFirstLoaded = true;
      _lastCommentarySlot = slot;
      renderSectors();                 // 首屏：全量渲染 + 拉 LLM 结论 + 精算回填
    } else {
      updateSectorsInPlace();          // 周期刷新：仅就地更新指标变化，不重拉结论
      // 档位变化（如午间→下午开盘）才补拉一次 LLM 板块结论
      if(slot && slot !== _lastCommentarySlot){
        _lastCommentarySlot = slot;
        fetchSecCommentary(sectorCache.filter(s=>s.watched));
      }
    }
  } catch(e) {
    console.warn('板块数据加载失败:', e);
    $('secGrid').innerHTML = '<div class="empty-hint">板块数据加载失败，请稍后重试</div>';
  }
}
function renderSectors(){
  // regime 横幅（V5：战略叠加层 + 当前 Regime + 评述标签 + 开关同一行）
  const rb = $('regimeBanner');
  const noteEl = $('regimeNote');
  if(currentRegime){
    $('regimeText').textContent = currentRegime;
    rb.style.display='flex';
  } else {
    $('regimeText').textContent = '未加载';
    rb.style.display='flex';   // 即使 Regime 为空也显示横幅（保留开关与评述标签）
  }
  // 默认评述状态，fetchSecCommentary 会异步更新
  if(noteEl){
    noteEl.textContent = '当前时段暂无评述';
    noteEl.classList.add('empty');
  }

  const secs = sectorCache;
  // 蓝框摘要：覆盖板块数 + 战略关注数
  _sectorTotal = secs.length;
  _overlayFavor = secs.filter(s => s.overlay_tag === 'favor').length;
  updateRegimeSummary();
  if(!secs.length){
    $('secGrid').innerHTML = '<div class="empty-hint">暂无板块数据</div>';
    $('loadMoreWrap').style.display='none';
    $('secGridMore').style.display='none';
    return;
  }

  // 已关注板块固定显示在主网格，未关注折叠进“更多板块”
  const watched = secs.filter(s => s.watched);
  const unwatched = secs.filter(s => !s.watched);
  $('secGrid').innerHTML = watched.length
    ? watched.map(s => renderSecCard(s, true)).join('')
    : '<div class="empty-hint">暂无关注板块，搜索板块后点击结果即可添加关注</div>';

  $('loadMoreBtn').textContent = '更多板块 (' + unwatched.length + ') ▼';
  if(unwatched.length){
    $('loadMoreWrap').style.display='flex';
  } else {
    $('loadMoreWrap').style.display='none';
    $('secGridMore').style.display='none';
    moreShown = false;
  }
  updateWatchHint();

  // 主网格已关注板块异步精算回填（不阻塞首屏渲染）
  const need = watched.filter(s => !s.summary && !s.enriched).map(s => s.bk);
  if(need.length) enrichBoards(need);

  if(moreShown && unwatched.length){
    const more = $('secGridMore');
    more.innerHTML = unwatched.map(s=>renderSecCard(s, false)).join('');
    more.style.display='grid';
    $('loadMoreBtn').textContent='收起板块 ▲';
  }

  // 异步拉取系统评述（只传当前显示的板块）
  fetchSecCommentary(moreShown ? secs : watched);
}
// 主力净流入文案：netLineText（周期刷新就地更新复用）
function netLineText(s){
  const net = s.main_net;
  if(net == null) return '主力 -';
  const netAbs = Math.abs(net);
  const netWord = net >= 0 ? '净流入' : '净流出';
  return `主力${netWord}${(netAbs/1e8).toFixed(2)}亿`;
}
/* 周期刷新：只更新已渲染卡片的指标（涨跌幅/净流入/状态/资金色带），不重拉 LLM 结论与精算 */
function updateSectorsInPlace(){
  const secs = sectorCache;
  _sectorTotal = secs.length;
  _overlayFavor = secs.filter(s => s.overlay_tag === 'favor').length;
  updateRegimeSummary();
  updateWatchHint();
  secs.forEach(s=>{
    const card = document.getElementById('sec-'+s.bk);
    if(!card) return;
    const fb = flowBand(s.main_net, s.amount);
    card.className = 'sec' + ((s.state==='主推')?' main':'') + ' ' + fb.cls;
    const pcEl = card.querySelector('.pc');
    if(pcEl){ pcEl.textContent = fmtPct(s.pct)+'%'; pcEl.className = 'pc '+cls(s.pct,'up'); }
    const netEl = card.querySelector('.net');
    if(netEl) netEl.textContent = netLineText(s);
    const stEl = card.querySelector('.st');
    if(stEl) stEl.textContent = s.state || s.label || '';
  });
}
function renderSecCard(s, isFirstBatch){
  const name = s.name || '';
  const pct = s.pct || 0;
  const amount = s.amount || 0;
  const net = s.main_net || 0;
  const state = s.state || s.label || '';
  const fb = flowBand(net, amount);
  const mainCls = state === '主推' ? ' main' : '';
  const star = `<span class="star ${s.watched ? '' : 'off'}" title="${s.watched ? '取消关注' : '点击关注'}" onclick="toggleWatchFromCard(event,'${esc(s.bk)}')">${s.watched ? '★' : '☆'}</span>`;

  // 角标：战略关注/回避
  let flagHtml = '';
  if(s.overlay_tag === 'favor'){
    flagHtml = ' <span class="flag flag-favor">战略关注</span>';
  } else if(s.overlay_tag === 'avoid'){
    flagHtml = ' <span class="flag flag-avoid">战略回避</span>';
  }

  // 净流入文字
  const netLine = netLineText(s);

  // 单句总结：首屏先用占位，精算后回填；折叠卡无总结
  const rawSummary = s.summary || '';
  let summary = rawSummary;
  const am = rawSummary.match(/【分析】([\s\S]*?)(?=【|$)/);
  if(am) summary = am[1].replace(/\s+/g,' ').trim();
  let sumHtml;
  if(summary) sumHtml = `<div class="sum">${esc(summary)}</div>`;
  else if(isFirstBatch) sumHtml = `<div class="sum loading">解读加载中…</div>`;
  else sumHtml = '';

  return `<div class="sec${mainCls} ${fb.cls}" id="sec-${esc(s.bk)}" data-bk="${esc(s.bk)}" data-name="${esc(name)}" onclick="onSecCardClick('${esc(s.bk)}','${esc(name)}')">
    <span class="st">${esc(state)}</span>
    ${star}
    <div class="nm">${esc(name)}${flagHtml}</div>
    <div class="pc ${cls(pct,'up')}">${fmtPct(pct)}%</div>
    <div class="net">${esc(netLine)}</div>
    ${sumHtml}
  </div>`;
}
// 资金左部色带：色相=方向（红=净流入/绿=净流出），深浅=占成交额比例；5档离散[6,3.5,1.5,0.6,0]%，<0.6%或无效→无色带
function flowBand(net, amount){
  if(amount <= 0) return {cls:''};
  const r = Math.abs(net) / amount * 100;
  if(r < 0.6) return {cls:''};
  const lvl = r >= 6 ? 1 : r >= 3.5 ? 2 : r >= 1.5 ? 3 : 4;
  return {cls: 'flow-' + (net >= 0 ? 'in' : 'out') + '-' + lvl};
}
async function enrichBoards(bks){
  try{
    const d = await api('/api/sector_enrich', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({bks}), timeout: 120000
    });
    const data = (d && d.data) || {};
    bks.forEach(bk=>{
      const info = data[bk];
      if(!info) return;
      const entry = sectorCache.find(s=>s.bk===bk);
      if(entry){
        entry.summary = info.summary || entry.summary;
        if(info.state) entry.state = info.state;
        entry.enriched = true;
      }
      const card = document.getElementById('sec-'+bk);
      if(card){
        card.dataset.enriched = '1';
        const sumEl = card.querySelector('.sum');
        if(info.summary){
          let sm = info.summary;
          const am = sm.match(/【分析】([\s\S]*?)(?=【|$)/);
          if(am) sm = am[1].replace(/\s+/g,' ').trim();
          if(sumEl){ sumEl.classList.remove('loading'); sumEl.textContent = sm; }
        }
        if(info.state){
          const stEl = card.querySelector('.st');
          if(stEl) stEl.textContent = info.state;
          if(info.state === '主推') card.classList.add('main');
        }
      }
    });
  }catch(e){ console.warn('enrich fail', e); }
}
/* ---- 板块系统评述（LLM 一句话总结，按时段缓存；并入 regime 蓝框，不再单独黄框）---- */
let _secCommentaryPollTimer = null;
let _secCommentaryRetries = 0;
const SEC_COMMENTARY_MAX_RETRIES = 6;
async function fetchSecCommentary(displayedSecs){
  const noteEl = $('regimeNote');
  const comEl = $('regimeCommentary');
  if(!displayedSecs || !displayedSecs.length){
    if(noteEl){ noteEl.textContent='当前时段暂无评述'; noteEl.classList.add('empty'); }
    if(comEl){ comEl.style.display='none'; comEl.textContent=''; }
    _secCommentaryRetries = 0;
    return;
  }
  if(_secCommentaryPollTimer){ clearTimeout(_secCommentaryPollTimer); _secCommentaryPollTimer = null; }
  try {
    // 首次显示加载态；后台生成中保留加载提示
    if(!comEl || !comEl.textContent.includes('生成中')){
      if(comEl){ comEl.style.display=''; comEl.innerHTML = '<span class="rc-time">系统评述生成中…</span>'; }
    }
    if(noteEl){ noteEl.textContent='评述生成中…'; noteEl.classList.add('empty'); }
    const d = await api('/api/sector_commentary', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({sectors: displayedSecs}), timeout: 90000
    });
    if(d.text){
      // ★B：每板块 LLM 简评回填卡片 .sum
      if(d.briefs && displayedSecs){
        displayedSecs.forEach(s=>{
          const bk = s.bk;
          const brief = d.briefs[bk];
          const card = document.getElementById('sec-'+bk);
          if(card){
            const sumEl = card.querySelector('.sum');
            if(brief && sumEl){ sumEl.classList.remove('loading'); sumEl.textContent = brief; }
          }
        });
      }
      if(comEl){
        const t = d.slot ? (d.slot.split('_')[1] || '') : '';
        const tag = d.cached ? (d.stale ? '（上一档·缓存）' : '（缓存）')
                            : (d.background ? '（生成中…）' : '');
        const timeHtml = t ? `<span class="rc-time">${esc(t)}档${tag}</span>`
                           : (tag ? `<span class="rc-time">${esc(tag)}</span>` : '');
        comEl.innerHTML = esc(d.text) + timeHtml;
        comEl.style.display='';
      }
      if(noteEl){
        const t = d.slot ? (d.slot.split('_')[1] || '') : '';
        if(d.background){
          noteEl.textContent = 'LLM简评后台生成中…';
          noteEl.classList.add('empty');
        } else {
          noteEl.textContent = t ? (t+'档'+(d.cached?'·缓存':'')) : (d.stale ? '上一档缓存' : '评述');
          noteEl.classList.remove('empty');
        }
      }
      // 后台生成中：轮询，最多 6 次（约 1.2 分钟）
      if(d.background && _secCommentaryRetries < SEC_COMMENTARY_MAX_RETRIES){
        _secCommentaryRetries++;
        _secCommentaryPollTimer = setTimeout(()=>{
          fetchSecCommentary(displayedSecs);
        }, 12000);
      } else {
        _secCommentaryRetries = 0;
      }
    } else {
      if(comEl){ comEl.style.display='none'; comEl.textContent=''; }
      if(noteEl){
        noteEl.textContent = d.note || '当前时段暂无评述';
        noteEl.classList.add('empty');
      }
      _secCommentaryRetries = 0;
    }
  } catch(e){
    console.warn('板块评述拉取失败:', e);
    if(comEl){ comEl.style.display='none'; }
    if(noteEl){ noteEl.textContent='评述暂时不可用'; noteEl.classList.add('empty'); }
    _secCommentaryRetries = 0;
  }
}
async function onSecCardClick(bk, name){
  const card = document.getElementById('sec-'+bk);
  if(card && !card.dataset.enriched) enrichBoards([bk]);
  loadBoardPicks(name, bk);
}
const LABEL_RANK = {"主推":0, "观察":1, "观望":2};
function sortSectors(){
  const watched = sectorCache.filter(s=>s.watched).map(s=>s.bk);
  const wmap = {}; watched.forEach((b,i)=>wmap[b]=i);
  const arr = sectorCache.slice();
  arr.sort((a,b)=>{
    if(a.watched !== b.watched) return a.watched ? -1 : 1;
    if(a.watched && b.watched) return wmap[a.bk]-wmap[b.bk];
    const la = (a.label && LABEL_RANK[a.label]!=null)?LABEL_RANK[a.label]:3;
    const lb = (b.label && LABEL_RANK[b.label]!=null)?LABEL_RANK[b.label]:3;
    if(la!==lb) return la-lb;
    const ra=(a.inflow_ratio||0), rbv=(b.inflow_ratio||0);
    if(ra!==rbv) return rbv-ra;
    return (b.pct||0)-(a.pct||0);
  });
  sectorCache = arr;
}
function updateWatchHint(){
  const n = savedWatched.size;
  const shown = sectorCache.filter(s=>s.watched).length;
  const el = $('watchHint');
  if(n===0){ el.style.display='none'; return; }
  el.style.display='inline';
  el.className = 'watch-hint';
  el.textContent = n === shown ? '已关注 '+n+' 个' : '已关注 '+n+' 个（主矩阵显示 '+shown+' 个）';
}
function onBoardSearch(){
  const q = $('boardSearch').value.trim();
  const box = $('searchSuggest');
  if(!q){ box.style.display='none'; return; }
  const matches = sectorCache.filter(s => s.name && s.name.indexOf(q) >= 0).slice(0, 12);
  if(!matches.length){ box.style.display='none'; return; }
  box.innerHTML = matches.map(s =>
    `<div class="sg-item" onclick="toggleWatch('${esc(s.bk)}')">${s.watched?'★ ':'☆ '}${esc(s.name)} <span class="sg-tag">${esc(s.state||s.label||'')}</span></div>`
  ).join('');
  box.style.display='block';
}
function toggleWatch(bk){
  const entry = sectorCache.find(s=>s.bk===bk);
  if(!entry) return;
  entry.watched = !entry.watched;
  toast(entry.watched ? '已关注：' + entry.name : '已取消关注：' + entry.name);
  const bkStr = String(bk);
  if(entry.watched) savedWatched.add(bkStr); else savedWatched.delete(bkStr);
  const bks = [...savedWatched];
  api('/api/watched_boards', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({bks})}).catch(e=>console.warn('watch save fail',e));
  sortSectors();
  renderSectors();
  $('boardSearch').value='';
  $('searchSuggest').style.display='none';
}
function toggleWatchFromCard(ev, bk){
  ev.stopPropagation();
  toggleWatch(bk);
}
let moreShown = false;
function toggleMore(){
  const more = $('secGridMore');
  const unwatched = sectorCache.filter(s=>!s.watched);
  if(moreShown){
    more.style.display='none';
    $('loadMoreBtn').textContent = '更多板块 (' + unwatched.length + ') ▼';
    moreShown=false;
    // 收起后评述只覆盖主网格
    fetchSecCommentary(sectorCache.filter(s=>s.watched));
    return;
  }
  if(!unwatched.length) return;
  more.innerHTML = unwatched.map(s=>renderSecCard(s, false)).join('');
  more.style.display='grid';
  $('loadMoreBtn').textContent='收起板块 ▲';
  moreShown=true;
  // 展开后评述覆盖全量
  fetchSecCommentary(sectorCache);
}
document.addEventListener('click', e=>{
  if(!e.target.closest('.sec-toolbar')) $('searchSuggest').style.display='none';
});

/* ---------- 区3：个股买点（点板块加载）---------- */
let _picksCache = null;   // /api/picks 全量扫描缓存（板块/搜索复用，避免重复拉）
const _C5LABEL = [
  ['c1','①周RSI>50'], ['c2','②日MA20>MA40'], ['c3a','③a 60分金叉'],
  ['c3b','③b 缩量回踩'], ['c4','④ 15分收敛'], ['c5','⑤ 15分启动'],
];
/* ---------- 设置面板「个股筛选」接线（2026-08-06）----------
   原实现只把筛选值写进 localStorage，后端不收、前端也不用 → UI 存在但不生效。
   现统一在 /api/picks 数据入口处套用，板块展开 / 查买点 / 个股表全部生效。
   市值需额外行情字段（picks 不返回），只在用户真的选了档位时才按需批量拉取。 */
function _getFilter(){
  try{ return JSON.parse(localStorage.getItem('apanel_filter')||'{}'); }catch(e){ return {}; }
}
let _mcapCache = {};      // secid -> 总市值(亿)
function _boardOf(code){
  const c = String(code||'');
  if(/^(688|689|787)/.test(c)) return 'kc';    // 科创
  if(/^(300|301)/.test(c))     return 'cy';    // 创业
  if(/^(8|4|920)/.test(c))     return 'bj';    // 北交
  return 'main';                               // 沪深主板
}
function _passFilter(r, f){
  const code = String(r.secid||'').split('.').pop();
  const b = _boardOf(code);
  const px = Number(r.close);
  if(f.priceMin!=null && f.priceMin!=='' && isFinite(px) && px < Number(f.priceMin)) return false;
  if(f.priceMax!=null && f.priceMax!=='' && isFinite(px) && px > Number(f.priceMax)) return false;
  if(f.main === false && b === 'main') return false;
  if(f.chiNext === false && (b === 'kc' || b === 'cy')) return false;
  if(!f.st && /ST|退/i.test(String(r.name||''))) return false;   // 默认不含 ST
  if(f.mcap){
    const mv = _mcapCache[r.secid];
    if(mv != null){          // 取不到市值的票放行，不误杀
      if(f.mcap==='small' && !(mv < 100)) return false;
      if(f.mcap==='mid'   && !(mv >= 100 && mv <= 500)) return false;
      if(f.mcap==='large' && !(mv > 500)) return false;
    }
  }
  return true;
}
function applyFilter(rows){
  const f = _getFilter();
  if(!f || !Object.keys(f).length) return rows || [];
  return (rows||[]).filter(r => _passFilter(r, f));
}
async function _ensureMcap(data){
  const ids = new Set();
  ['full_match','pool','seq8_candidates','seq8_triggers','seq8_others'].forEach(k=>{
    (data[k]||[]).forEach(r=>{ if(r.secid && _mcapCache[r.secid]==null) ids.add(r.secid); });
  });
  if(!ids.size) return;
  try{
    const d = await api('/api/mktcap?secids=' + encodeURIComponent([...ids].join(',')), {timeout: 20000});
    Object.assign(_mcapCache, (d && d.mv) || {});
  }catch(e){ console.warn('市值筛选数据拉取失败，该项本次不生效:', e); }
}
function _loadPicksData(){
  return api('/api/picks').then(async d => {
    const data = (d && d.data) ? d.data : null;
    if(data){
      const f = _getFilter();
      if(f && f.mcap) await _ensureMcap(data);
      ['full_match','pool','seq8_candidates','seq8_triggers','seq8_others'].forEach(k=>{
        if(Array.isArray(data[k])) data[k] = applyFilter(data[k]);
      });
    }
    _picksCache = data;
    return _picksCache;
  });
}
async function loadBoardPicks(boardName, bk){
  const emptyEl = $('stockEmpty');
  const tableEl = $('stockTable');
  const bodyEl = $('stockBody');

  emptyEl.style.display='none';
  tableEl.style.display='';
  $('stockBoardName').textContent = boardName + ' · 板块内五条件共振个股（序贯状态机）';
  bodyEl.innerHTML = '<tr><td colspan="6" style="text-align:center;padding:20px;color:var(--sub)">加载中...</td></tr>';

  try {
    const data = _picksCache || await _loadPicksData();
    if(!data){
      bodyEl.innerHTML = '<tr><td colspan="6" style="text-align:center;padding:20px;color:var(--sub)">扫描缓存为空（后台可能在算，稍后重试）</td></tr>';
      return;
    }
    // 该板块全部票 = full_match ∪ pool ∪ seq8_candidates ∪ seq8_triggers（按 bk 过滤）
    const all = [].concat(data.full_match||[], data.pool||[],
                          data.seq8_candidates||[], data.seq8_triggers||[]);
    const rows = bk ? all.filter(r => r.bk === bk) : all;
    renderPickRows(rows, boardName);
  } catch(e) {
    bodyEl.innerHTML = '<tr><td colspan="6" style="text-align:center;padding:20px;color:var(--sub)">加载失败，请重试</td></tr>';
  }
}
function stTag(secid){
  // ★E：双创标签（688→科 / 300·301→创）
  const code = (secid||'').split('.')[1] || '';
  if(/^688/.test(code)) return '<span class="tag-kc">科</span>';
  if(/^(300|301)/.test(code)) return '<span class="tag-cy">创</span>';
  return '';
}
function renderPickRows(rows, boardName){
  const body = $('stockBody');
  if(!rows.length){
    body.innerHTML = '<tr><td colspan="6" style="text-align:center;padding:20px;color:var(--sub)">该板块暂无符合五条件的个股</td></tr>';
    return;
  }
  body.innerHTML = rows.map(p => {
    const name = p.name || '';
    const dispCode = (p.secid||'').split('.')[1] || p.secid;
    const cm = _parseSecid(p.secid);
    const isHold = _isHolding(cm.code, cm.market);
    const close = (p.close!=null) ? p.close : '--';
    const pct = (p.pct!=null) ? p.pct : 0;
    const tier = p.tier || 4;
    const stage = p.stage || 'idle';
    let stageTxt, stageCls, stageClsLv;
    if(stage === 'triggered'){ stageTxt='⑤今日响·可上车'; stageCls='trig'; stageClsLv='触发'; }
    else if(stage === 'waiting'){ stageTxt=`④已就绪·剩${p.days_left}日`; stageCls='observe'; stageClsLv='观察'; }
    else { stageTxt='观察'; stageCls='stk'; stageClsLv='无'; }

    // 五条件逐条通过标记
    const c5 = _C5LABEL.map(([k,lab])=>`<em class="${p[k]?'p':'f'}">${lab}</em>`).join('');

    // 序贯状态列
    let seqTxt;
    if(stage === 'waiting'){
      seqTxt = `④日 ${p.c4_date||'-'} · 已等${p.days_waited}日 · 窗口剩${p.days_left}日`;
    } else if(stage === 'triggered'){
      seqTxt = `⑤响日 ${p.fired_date||'-'}`;
    } else {
      const st = p.seq_stat || {};
      seqTxt = `就绪${st.arm||0}/同天${st.same||0}/跨日${st.cross||0}/破${st.break||0}/超时${st.expire||0}`;
    }
    const rsiTxt = `周${p.wk_rsi!=null?p.wk_rsi:'-'} / 日${p.d_rsi!=null?p.d_rsi:'-'}`;
    const tierBadge = `<span class="badge ${tier<=2?'main':'other'}">T${tier}</span>`;
    const trend = p.trend_type || '';
    const trendBadge = trend === 'main' ? ' <span class="lc-trend main">主升</span>' :
                       (trend === 'early' ? ' <span class="lc-trend early">初期</span>' : '');

    return `<tr class="${stageCls}">
      <td class="l"><span class="stock-star ${isHold?'on':''}" data-holding-star data-code="${esc(cm.code)}" data-market="${cm.market}" onclick="toggleHolding('${esc(p.secid)}','${esc(name)}',event)">${isHold?'★':'☆'}</span><b>${esc(name)}</b>${trendBadge} ${tierBadge} ${stTag(p.secid)}<br><span class="lc-code">${esc(dispCode)}</span></td>
      <td>${esc(close)}<br><span class="${cls(pct,'up')}" style="font-size:11px">${fmtPct(pct)}%</span></td>
      <td><div class="sig">${c5}</div></td>
      <td><span class="lv ${stageClsLv}">${stageTxt}</span></td>
      <td class="l"><div class="sig" style="margin:0">${esc(seqTxt)}</div></td>
      <td class="l"><div class="lc-meta">${rsiTxt}</div></td>
    </tr>`;
  }).join('');
}

/* ---------- 区4：状态机漏斗 ---------- */
async function loadLifecycle(){
  try {
    const d = await api('/api/lifecycle');
    const groups = (d && d.groups) || {};
    renderPipeline(d);
    notifyReadyChanges(groups.ready || []);
  } catch(e) {
    console.warn('生命周期数据加载失败:', e);
  }
}
function renderPipeline(d){
  const groups = (d && d.groups) || {};
  const ready = groups.ready || [];   // 待上车列表
  const watchRaw = groups.watch || [];   // 观察池原始列表
  // ★展示门槛（2026-08-05）：最笨逻辑——观察池至少得有 ① ② 和 ③a 命中才展示。
  // 系统无手动加入通道（channel B 恒为 0），所有条目统一按 ①②③ 门槛过滤；全叉则隐藏。
  const watch = watchRaw.filter(t => t.c1 && t.c2 && t.c3a);

  // ---- 待上车（顶，按板块分组）----
  const rStage = $('lcReadyStage');
  const rCnt = $('readyCnt');
  const rContent = $('lcReadyContent');
  if(ready.length){
    rStage.style.display='';
    rCnt.textContent = ready.length;
    const groupsMap = {};
    ready.forEach(item => {
      const b = item.sector || item.bk || '未分类';
      if(!groupsMap[b]) groupsMap[b]=[];
      groupsMap[b].push(item);
    });
    const sortedGroups = Object.entries(groupsMap)
      .map(([name, items]) => ({name, items, weight: 0}))
      .sort((a,b) => b.weight - a.weight);

    rContent.innerHTML = sortedGroups.map(g => `
      <div class="board-group">
        <div class="bg-head"><span class="bg-name">${esc(g.name)}</span><span class="bg-w">权重 ${(g.weight||0).toFixed(1)}</span><span class="bg-cnt">${g.items.length} 支</span></div>
        <div class="lc-grid">
          ${g.items.map(item => renderLcCard(item, true)).join('')}
        </div>
      </div>`).join('');
  } else {
    rStage.style.display='none';
  }

  // ---- 观察池（底，T1/T2置顶 + T3/T4滚动）----
  const wStage = $('lcWatchStage');
  const wCnt = $('watchCnt');
  const wContent = $('lcWatchContent');
  if(watch.length){
    wStage.style.display='';
    wCnt.textContent = watch.length;
    _watchTotal = watch.length;
    updateRegimeSummary();
    // 同 tier 内按五条件命中条数降序，命中越多越靠前（如龙净环保 ①②③④ 排在同 tier 前面）
    const sortedWatch = [...watch].sort((a,b) => {
      const ta = (a.tier||9), tb = (b.tier||9);
      if(ta !== tb) return ta - tb;
      return (b.hit_bits||'').length - (a.hit_bits||'').length;
    });
    const topItems = sortedWatch.filter(t => (t.tier||9) <= 2);
    const restItems = sortedWatch.filter(t => (t.tier||9) > 2);

    let html = '';
    if(topItems.length){
      html += `<div class="lc-lead"><span>头部关注 · T1 / T2</span><span class="bar"></span></div>`;
      html += `<div class="lc-grid">${topItems.map(item => renderLcCard(item, false)).join('')}</div>`;
    }
    if(restItems.length){
      html += `<div class="lc-scroll">
        <div class="lc-lead flat"><span>其余 · T3 / T4</span><span class="bar"></span></div>
        <div class="lc-grid">${restItems.map(item => renderLcCard(item, false)).join('')}</div>
      </div>`;
    }
    wContent.innerHTML = html;
  } else {
    wStage.style.display='none';
  }
}
function renderLcCard(item, isReady){
  const name = item.name || '';
  const dispCode = (item.secid||'').split('.')[1] || item.secid;   // 显示真实代码，非 secid
  const cm = _parseSecid(item.secid);
  const isHold = _isHolding(cm.code, cm.market);
  const ch = item.channel || 'A';   // A/B通道
  const tier = item.tier || 4;       // 数值 1-4（非 "T1" 字符串）
  const chCls = ch === 'A' ? 'A' : 'B';
  // ★2026-08-05 双路径标签：主升 / 上涨初期
  const trend = item.trend_type || '';
  const trendCls = trend === 'main' ? 'main' : (trend === 'early' ? 'early' : '');
  const trendTxt = trend === 'main' ? '主升' : (trend === 'early' ? '初期' : '');
  const sector = item.sector || '';
  const sectorHtml = (!isReady && sector) ? `<span class="lc-sector">${esc(sector)}</span>` : '';
  let priceHtml = '';
  if(!isReady && item.close != null){
    const pctTxt = (item.pct != null) ? ' ' + fmtPct(item.pct) + '%' : '';
    priceHtml = `<span class="lc-price">现价 ${Number(item.close).toFixed(2)}${pctTxt}</span>`;
  }

  // 五条件命中条数（V5 样式）
  const bits = item.hit_bits || '';
  let hitTxt = '';
  if(bits){
    // ★C8：⑤ 触发（①②③⑤ 齐，④ 为已就绪的时序闸门）显示为「⑤触发·待上车」，不再标「全命中」
    // ★2026-08-06：待上车必须核验前置条件 c4_armed（④曾就绪）——后端 entry 要求 armed+base_ok，
    //   前端 bits 推导不能绕过；合并条目带 armed，落盘条目回退用 seq8_state(waiting/triggered 隐含就绪)
    const filtered = item.entry_filtered || item.entry_ok === false;
    const armed = !!(item.armed || item.seq8_state === 'waiting' || item.seq8_state === 'triggered');
    const trig = !filtered && ((item.seq8_state === 'triggered') || item.entry ||
                 (bits.includes('①') && bits.includes('②') && bits.includes('③') && bits.includes('⑤') && armed));
    hitTxt = bits + ' ' + (filtered ? '⑤触发·远离MA5·已过滤' : (trig ? '⑤触发·待上车' : '命中'));
  }

  // 序贯状态说明（作为次 meta）
  let seqInfo = '';
  const st = item.seq8_state || 'idle';
  if(st === 'waiting'){
    seqInfo = '④窗口剩 ' + (item.seq8_deadline ? '至'+item.seq8_deadline : '-') +
              (item.last_c4_date ? ' · ④日'+item.last_c4_date : '');
  } else if(st === 'triggered'){
    seqInfo = '⑤响日 ' + (item.seq8_trigger_date || '-') +
              (item.last_c4_date ? ' · ④日'+item.last_c4_date : '');
  } else if(item.entry_filtered){
    seqInfo = '⑤已响但远离MA5·等待回踩';
  } else if(st === 'voided'){
    seqInfo = '破位作废';
  } else if(st === 'expired'){
    seqInfo = '窗口超时';
  } else {
    seqInfo = '基座观察中';
  }
  const meta = hitTxt || (item.meta || item.desc || item.note || '') || seqInfo;

  let actions = '';
  if(item.merged_pool){
    // ★2026-08-06：合并池条目（今日扫描临时合并）提供「加入观察池」=落盘为正式观察条目
    actions = `<div class="lc-actions">
      <button class="ghost" onclick="event.stopPropagation();addToWatch('${esc(item.secid)}','${esc(name)}')">固定观察池显示</button>
    </div>`;
  } else if(isReady){
    actions = `<div class="lc-actions">
      <button class="primary" onclick="event.stopPropagation();confirmBuy('${esc(item.secid)}')">确认上车</button>
      <button class="ghost" onclick="event.stopPropagation();removeFromReady('${esc(item.secid)}')">移出</button>
    </div>`;
  } else {
    actions = `<div class="lc-actions">
      <button class="ghost" onclick="event.stopPropagation();removeFromWatch('${esc(item.secid)}')">移出</button>
    </div>`;
  }

  // 点击卡片 → 跳转个股详情 preview 页（合并池条目同样可点开看详情，仅不提供操作按钮）
  const clickable = !!item.secid;
  const cardCls = clickable ? 'lc-card lc-clickable' : 'lc-card';
  const cardAttr = clickable ? ` onclick="focusStock('${esc(item.secid)}','${esc(name)}')"` : '';

  return `<div class="${cardCls}${isHold?' holding':''}"${cardAttr}>
    <div class="lc-name"><span class="lc-star ${isHold?'on':''}" data-holding-star data-code="${esc(cm.code)}" data-market="${cm.market}" onclick="toggleHolding('${esc(item.secid)}','${esc(name)}',event)">${isHold?'★':'☆'}</span>${esc(name)} <span class="lc-code">${esc(dispCode)}</span> ${stTag(item.secid)} ${sectorHtml} <span class="lc-ch ${chCls}">${ch}</span>${trendTxt ? ` <span class="lc-trend ${trendCls}">${trendTxt}</span>` : ''}</div>
    <span class="lc-tier">T${tier}</span>
    <div class="lc-meta">${priceHtml}${esc(meta)}</div>
    ${actions}
  </div>`;
}

/* ---------- 观察池/待上车卡片点击 → 打开个股详情 preview 页 ---------- */
function focusStock(secid, name){
  if(!secid) return;
  const parts = secid.split('.');
  const code = parts[1] || parts[0] || '';
  if(!/^\d{6}$/.test(code)) return;
  // 沪市（6/8/9 开头）market=1，深市（0/3 开头）market=0；与后端 _resolve_secid 规则一致
  const market = /^[689]/.test(code) ? 1 : 0;
  const url = '/preview?code=' + encodeURIComponent(code) +
              '&market=' + market +
              '&name=' + encodeURIComponent(name || '') +
              '&v=20260806a';
  const f = $('previewFrame');
  if(f) f.src = url;
  openModal('previewModal');
}

/* ---------- 操作按钮（对接后端 lifecycle 接口，真实持久化；仍不自动下单）---------- */
async function confirmBuy(secid){
  if(!confirm('确认将 '+secid+' 标记为「上车」？\n⚠️ 本工具不自动下单，仅记录意图。')) return;
  try {
    await api('/api/lifecycle/board', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({secid, entry_price:'', entry_date:'', shares:''})});
    loadLifecycle();
  } catch(e){ alert('操作失败：'+e.message); }
}
async function removeFromReady(secid){
  if(!confirm('从待上车移出 '+secid+'？')) return;
  try {
    await api('/api/lifecycle/remove', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({secid})});
    loadLifecycle();
  } catch(e){ alert('操作失败：'+e.message); }
}
async function removeFromWatch(secid){
  if(!confirm('从观察池移出 '+secid+'？')) return;
  try {
    await api('/api/lifecycle/remove', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({secid})});
    loadLifecycle();
  } catch(e){ alert('操作失败：'+e.message); }
}

// ★2026-08-06：合并池条目（今日扫描临时合并）→ 加入观察池（落盘为通道B正式条目）
async function addToWatch(secid, name){
  try {
    const r = await api('/api/lifecycle/add', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({secid, name})});
    if(r && r.error) { alert(r.error); return; }
    loadLifecycle();
  } catch(e){ alert('操作失败：'+e.message); }
}

/* ---------- 查买点历史记忆（localStorage）---------- */
const STOCK_HIST_KEY = 'apanel_stock_history';
const STOCK_HIST_MAX = 20;
let stockHistTimer = null;
function loadStockHist(){ try{ return JSON.parse(localStorage.getItem(STOCK_HIST_KEY)) || []; }catch(e){ return []; } }
function saveStockHist(list){ try{ localStorage.setItem(STOCK_HIST_KEY, JSON.stringify(list)); }catch(e){} }
function secidToParts(secid){
  const parts = String(secid||'').split('.');
  const code = parts[1] || parts[0] || '';
  const market = /^[689]/.test(code) ? 1 : 0;
  return { code, market };
}
function addStockHist(secid, name){
  const { code, market } = secidToParts(secid);
  if(!/^\d{6}$/.test(code)) return;
  let list = loadStockHist().filter(x => x.code !== code);
  list.unshift({ code, market, name: name || code, ts: Date.now() });
  if(list.length > STOCK_HIST_MAX) list = list.slice(0, STOCK_HIST_MAX);
  saveStockHist(list);
  renderStockHist();
}
function rmStockHist(code){
  saveStockHist(loadStockHist().filter(x => x.code !== code));
  renderStockHist();
}
function fmtHistTime(ts){
  const d = new Date(ts); const p = n => String(n).padStart(2,'0');
  return p(d.getHours())+':'+p(d.getMinutes());
}
function renderStockHist(){
  const el = $('stockHist'); if(!el) return;
  const list = loadStockHist();
  if(!list.length){ el.style.display='none'; return; }
  el.innerHTML = list.map(x=>{
    const tag = x.market===1 ? '沪' : '深';
    return `<div class="sg-item" data-code="${esc(x.code)}" data-market="${x.market}" data-name="${esc(x.name)}">`+
      `<span>${esc(x.name)} <span style="color:var(--sub);font-size:11px">${x.code}</span></span>`+
      `<span class="sg-tag">${tag} ${fmtHistTime(x.ts)}</span>`+
      `</div>`;
  }).join('') + `<div class="sg-item" id="stockHistClear" style="justify-content:center;color:var(--sub)">清空记录</div>`;
  el.querySelectorAll('.sg-item[data-code]').forEach(it=>{
    it.onmousedown = e=>{   // mousedown 先于 blur，避免点击时下拉消失
      e.preventDefault();
      const code = it.dataset.code;
      $('stockSearchInput').value = code;
      focusStock(`${it.dataset.market}.${code}`, it.dataset.name);
      hideStockHist(true);
    };
  });
  const clr = $('stockHistClear');
  if(clr) clr.onmousedown = e=>{ e.preventDefault(); if(confirm('清空全部查询记录？')){ saveStockHist([]); hideStockHist(true); } };
}
function showStockHist(){ if(stockHistTimer){ clearTimeout(stockHistTimer); stockHistTimer=null; } renderStockHist(); const el=$('stockHist'); if(el) el.style.display='block'; }
function hideStockHist(now){ if(stockHistTimer){ clearTimeout(stockHistTimer); } const el=$('stockHist'); if(!el) return; if(now){ el.style.display='none'; stockHistTimer=null; } else { stockHistTimer=setTimeout(()=>{ el.style.display='none'; stockHistTimer=null; }, 180); } }

/* ---------- 查买点（代码搜索）---------- */
async function doSearchStock(){
  const val = ($('stockSearchInput').value||'').trim();
  if(!val){ alert('请输入股票代码或名称'); return; }
  const btn = $('btnSearchStock');
  const oldText = btn ? btn.textContent : '';
  if(btn){ btn.disabled = true; btn.textContent = '查询中…'; }
  try {
    const q = val.toLowerCase();

    // 6 位代码直接实时查询，不依赖 picks 全市场扫描缓存
    if(/^\d{6}$/.test(q)){
      const d = await api('/api/stock_seq', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({q: val}), timeout: 30000});
      if(d.row){
        addStockHist(d.row.secid, d.row.name);
        focusStock(d.row.secid, d.row.name);
        return;
      }
      if(d.error){ alert('查询失败：'+d.error); return; }
      alert('未找到符合条件的结果');
      return;
    }

    const data = _picksCache || await _loadPicksData();
    const all = data ? [].concat(data.full_match||[], data.pool||[], data.seq8_candidates||[], data.seq8_triggers||[]) : [];
    const hits = all.filter(r => (r.secid && r.secid.toLowerCase().includes(q)) || (r.name && r.name.toLowerCase().includes(q)));
    if(hits.length){
      addStockHist(hits[0].secid, hits[0].name);
      focusStock(hits[0].secid, hits[0].name);
      return;
    }

    // 名称查询缓存未命中 → 实时查单票
    const d = await api('/api/stock_seq', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({q: val}), timeout: 30000});
    if(d.row){
      addStockHist(d.row.secid, d.row.name);
      focusStock(d.row.secid, d.row.name);
      return;
    }
    if(d.error){ alert('查询失败：'+d.error); return; }
    alert('未找到符合条件的结果');
  } catch(e){
    alert('查询失败：'+e.message);
  } finally {
    if(btn){ btn.disabled = false; btn.textContent = oldText; }
  }
}

/* ---------- 持仓弹窗 ---------- */
// 点击「我的持仓」时动态加载（同时拉取详细持仓 + 星标持仓）
document.querySelector('button[onclick*="positionsModal"]')?.addEventListener('click', async () => {
  try {
    const [pos, hol] = await Promise.all([api('/api/positions'), api('/api/holdings')]);
    renderPositions(pos, hol);
  } catch(e) {
    $('posHoldingArea').innerHTML = '<div class="empty-hint" style="padding:16px">持仓数据加载失败</div>';
  }
});
function renderPositions(d, hol){
  const holds = d.holdings || d.positions || [];
  const exits = d.exits || [];
  const holdings = (hol && hol.holdings) || [];

  let html = '';
  // 星标持仓（轻量标记，无需成本）
  if(holdings.length){
    html += `<div class="lc-lead"><span>⭐ 星标持仓</span><span class="bar"></span></div>`;
    html += `<div class="lc-grid">${holdings.map(h => {
      const secid = `${h.market}.${h.code}`;
      return `<div class="lc-card holding">
        <div class="lc-name"><span class="lc-star on" data-holding-star data-code="${esc(h.code)}" data-market="${h.market}">★</span>${esc(h.name||'')} <span class="lc-code">${esc(h.code||'')}</span></div>
        <div class="lc-meta">${h.close!=null ? `现价 ${fmtPrice(h.close)} / ${fmtPct(h.pct)}%` : ''} ${h.state && h.state.advice ? '· '+esc(h.state.advice) : ''}</div>
        <div class="lc-actions">
          <button class="ghost" onclick="toggleHolding('${esc(secid)}','${esc(h.name||'')}',event); setTimeout(()=>document.querySelector('button[onclick*=positionsModal]')?.click(),300)">取消星标</button>
        </div>
      </div>`;
    }).join('')}</div>`;
  }

  // 详细持仓（含成本、卖点信号）
  if(holds.length){
    if(html) html += `<div class="lc-lead" style="margin-top:14px"><span>📊 持仓明细</span><span class="bar"></span></div>`;
    html += `<div class="lc-grid">${holds.map(h => `
      <div class="lc-card">
        <div class="lc-name">${esc(h.name||'')} <span class="lc-code">${esc(h.code||'')}</span> <span class="lc-ch ${(h.channel||'A')}">${h.channel||'A'}</span></div>
        <div class="lc-meta">${esc(h.meta||h.desc||'')}</div>
        <div class="lc-actions">
          <button class="danger" onclick="confirmExit('${esc(h.secid||h.code||'')}', 1)">减仓</button>
          <button class="danger" onclick="confirmExit('${esc(h.secid||h.code||'')}', 3)">清仓</button>
        </div>
      </div>`).join('')}</div>`;
  }

  $('posHoldingArea').innerHTML = html || '<div class="empty-hint" style="padding:16px">暂无持仓</div>';

  // 提示离场
  if(exits.length){
    $('posExitArea').style.display='';
    $('posExitList').innerHTML = exits.map(h => `
      <div class="lc-card">
        <div class="lc-name">${esc(h.name||'')} <span class="lc-code">${esc(h.code||'')}</span></div>
        <span class="lc-badge-clear">${esc(h.exit_reason||'卖点信号')}</span>
        <div class="lc-meta">${esc(h.meta||'')}</div>
        <div class="lc-actions">
          <button class="danger" onclick="confirmExit('${esc(h.secid||h.code||'')}', 1)">减仓</button>
          <button class="danger" onclick="confirmExit('${esc(h.secid||h.code||'')}', 3)">清仓</button>
        </div>
      </div>`).join('');
  } else {
    $('posExitArea').style.display='none';
  }
}
async function confirmExit(secid, layer){
  const action = layer===3 ? '清仓' : '减仓';
  if(!confirm(`确认对 ${secid} 执行「${action}」？\n⚠️ 本工具不自动下单，仅记录意图。`)) return;
  try{
    await api('/api/lifecycle/exit', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({secid, layer})});
    // 刷新持仓弹窗数据
    const d = await api('/api/positions');
    renderPositions(d);
  }catch(e){ alert('操作失败：'+e.message); }
}

/* ---------- 做T信号（移植 a-trade，数据源沿用 HKS K线入口） ---------- */
async function scanTTrade(){
  if(_tScanning) return;
  _tScanning = true;
  const query = ($('tScanCode').value || '').trim();
  const msg = $('tScanMsg');
  if(msg) msg.textContent = '扫描中…';
  let body = {};
  if(query){
    if(/^\d{6}$/.test(query)){
      const market = /^[689]/.test(query) ? 1 : 0;
      body = {secids: [market + '.' + query]};
    } else {
      body = {q: query};
    }
  }
  try{
    const d = await api('/api/t_scan', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(body), timeout: 120000
    });
    renderTTrade(d);
    notifyTSignalChanges(d.items);
    if(msg){
      const n = (d.items||[]).reduce((a,it)=>a+(it.signals||[]).length,0);
      msg.textContent = d.note || ('完成：' + (d.items||[]).length + ' 只 / ' + n + ' 个信号');
    }
  }catch(e){
    if(msg) msg.textContent = '扫描失败：' + e.message;
  } finally {
    _tScanning = false;
  }
}

/* ---------- 消息通知（新做T信号 / 新待上车） ---------- */
function notifyPopup(title, body, sub, kind, secid, name){
  let stack = $('notifyStack');
  if(!stack){
    stack = document.createElement('div');
    stack.id = 'notifyStack';
    stack.className = 'notify-stack';
    document.body.appendChild(stack);
  }
  while(stack.children.length >= 4) stack.removeChild(stack.firstChild);

  const card = document.createElement('div');
  card.className = 'notify-card ' + (kind === 'ready' ? 'ready' : 't-signal');
  card.innerHTML = `<div class="notify-title"><span>${esc(title)}</span><span class="notify-close" title="关闭">×</span></div>` +
    `<div class="notify-body">${esc(body)}</div>` +
    (sub ? `<div class="notify-sub">${esc(sub)}</div>` : '');
  const dismiss = () => {
    if(card.classList.contains('out')) return;
    card.classList.add('out');
    setTimeout(() => { if(card.parentNode) card.parentNode.removeChild(card); }, 220);
  };
  card.querySelector('.notify-close').addEventListener('click', e => {
    e.stopPropagation();
    dismiss();
  });
  card.addEventListener('click', () => {
    if(secid && /^[01]\.\d{6}$/.test(secid) && name) focusStock(secid, name);
    dismiss();
  });
  stack.appendChild(card);
  setTimeout(dismiss, 8000);
}
function notifyReadyChanges(ready){
  if(!_readyNotifiedBaseline){
    _readyNotifiedBaseline = true;
    (ready || []).forEach(item => { if(item.secid) _knownReadyStocks.add(item.secid); });
    return;
  }
  (ready || []).forEach(item => {
    if(!item.secid || _knownReadyStocks.has(item.secid)) return;
    _knownReadyStocks.add(item.secid);
    const code = (item.secid || '').split('.')[1] || '';
    notifyPopup('待上车', (item.name || code || item.secid) + (code ? ' ' + code : ''),
      '⑤触发 · ' + (item.sector || '个股待上车'), 'ready', item.secid, item.name);
  });
}
function notifyTSignalChanges(items){
  const signals = [];
  (items || []).forEach(it => {
    (it.signals || []).forEach(s => {
      signals.push({
        key: [it.secid, s.signal_type, s.signal_name || ''].join('|'),
        it, s,
      });
    });
  });
  if(!_tsignalNotifiedBaseline){
    _tsignalNotifiedBaseline = true;
    signals.forEach(x => _knownTSignals.add(x.key));
    return;
  }
  signals.forEach(({key, it, s}) => {
    if(_knownTSignals.has(key)) return;
    _knownTSignals.add(key);
    const dir = s.signal_type === 'buy' ? '低吸' : (s.signal_type === 'sell' ? '高抛' : '止损');
    const sub = [dir, s.signal_name, s.reason].filter(Boolean).join(' · ');
    notifyPopup('新做T信号', (it.name || it.code) + ' ' + it.code, sub, 't-signal', it.secid, it.name);
  });
}
function tStrengthTxt(s){
  return {weak:'弱', medium:'中', strong:'强'}[s] || s || '-';
}
function tStateText(state){
  if(!state || state.status === 'empty') return '无T仓';
  if(state.status === 'holding'){
    return 'T仓 <b>' + Number(state.entry_price||0).toFixed(2) + '</b><br>' +
           esc(state.entry_signal || '低吸');
  }
  return 'T+1锁定';
}
function renderTTrade(d){
  const el = $('tContent');
  const items = d.items || [];
  const rows = [];
  items.forEach(it => (it.signals || []).forEach(s => rows.push({it, s})));
  const states = items.filter(it => it.state && it.state.status !== 'empty');

  let html = '';
  if(states.length){
    html += `<div class="lc-lead"><span>当日 T 仓</span><span class="bar"></span></div>
      <div class="lc-grid">${states.map(it => `
        <div class="lc-card holding">
          <div class="lc-name">${esc(it.name)} <span class="lc-code">${esc(it.code)}</span></div>
          <div class="lc-meta">${tStateText(it.state)}</div>
          <div class="lc-actions">
            <button class="ghost" onclick="markTState('${esc(it.code)}','exit')">平T仓</button>
          </div>
        </div>`).join('')}
      </div>`;
  }

  if(rows.length){
    html += `<div class="lc-lead"><span>做T信号</span><span class="bar"></span></div>
      <table>
        <thead><tr><th class="l">代码 / 名称</th><th>方向</th><th>信号</th><th>强度</th><th>触发价</th><th class="l">原因</th><th class="l">T仓状态</th><th class="l">操作</th></tr></thead>
        <tbody>${rows.map(({it, s}) => {
          const sigCls = s.signal_type === 'buy' ? 'buy' : (s.signal_type === 'sell' ? 'sell' : 'stop');
          const sigTxt = s.signal_type === 'buy' ? '低吸' : (s.signal_type === 'sell' ? '高抛' : '止损');
          const price = s.trigger_price != null ? Number(s.trigger_price).toFixed(2) : '--';
          let actions = '';
          if(s.signal_type === 'buy' && it.state && it.state.status !== 'holding'){
            actions += `<button class="ghost" onclick="markTState('${esc(it.code)}','buy',${price})">记入T仓</button>`;
          }
          if(it.state && it.state.status === 'holding' && (s.signal_type === 'sell' || s.signal_type === 'stop_loss')){
            actions += `<button class="ghost" onclick="markTState('${esc(it.code)}','exit')">平T仓</button>`;
          }
          return `<tr>
            <td class="l"><b>${esc(it.name)}</b><br><span class="lc-code">${esc(it.code)}</span></td>
            <td><span class="t-sig ${sigCls}">${sigTxt}</span></td>
            <td>${esc(s.signal_name)}</td>
            <td>${tStrengthTxt(s.strength)}</td>
            <td>${price}</td>
            <td class="l">${esc(s.reason || '')}</td>
            <td class="l">${tStateText(it.state)}</td>
            <td class="l">${actions || '-'}</td>
          </tr>`;
        }).join('')}</tbody>
      </table>`;
  } else if(!states.length){
    html = '<div class="empty-hint">' + (d.note || '本次扫描暂无做T信号') + '</div>';
  }
  el.innerHTML = html;
}
async function markTState(code, action, price){
  const isBuy = action === 'buy';
  if(isBuy && !confirm('记入当日 T 仓 @ ' + price + '？\n仅记录状态，不自动下单。')) return;
  if(!isBuy && !confirm('平掉 ' + code + ' 的当日 T 仓？')) return;
  try{
    await api('/api/t_state', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({code, action, price: isBuy ? Number(price||0) : undefined})});
    toast(isBuy ? '已记入T仓' : '已平T仓');
    scanTTrade();
  }catch(e){ alert('操作失败：' + e.message); }
}

/* ---------- 弹窗控制 ---------- */
function openModal(id){ $(id).classList.add('show'); }
function closeModal(id){ $(id).classList.remove('show'); }
async function openSettings(){
  try{
    const cfg = await api('/api/config');
    window.__apanelCfg = cfg;
    $('llmEnabled').checked = !!cfg.enabled;
    $('llmEndpoint').value = cfg.endpoint || '';
    $('llmModel').value = cfg.model || '';
    $('llmKey').value = '';   // 安全：不回填 key，留空 = 不修改已保存的 key
    $('llmTestMsg').textContent = cfg.has_key ? '（已保存 key，留空则不修改）' : '';
  }catch(e){ console.warn('加载 LLM 配置失败:', e); }
  openModal('settingsModal');
}
function friendlyLlmError(raw){
  if(!raw) return '未知错误';
  const s = String(raw);
  if(/ConnectionReset|10054|远程主机|强迫关闭/.test(s)) return 'AI 联通失败：连接被对端重置（代理未连通 / LLM 端点不可达）';
  if(/timed out|Timeout|超时/.test(s)) return 'AI 联通失败：请求超时（代理 / 网络未连通）';
  if(/Name or service not known|Failed to resolve|getaddrinfo/.test(s)) return 'AI 联通失败：无法解析 LLM 端点域名（检查 endpoint 或网络）';
  if(/Connection refused|10061|拒绝/.test(s)) return 'AI 联通失败：连接被拒绝（代理或 LLM 端点未启动）';
  return s;
}
async function testLlm(){
  const msg = $('llmTestMsg');
  msg.textContent = '测试中…';
  try{
    await api('/api/config', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({llm:{enabled:$('llmEnabled').checked, endpoint:$('llmEndpoint').value.trim(), api_key:$('llmKey').value.trim(), model:$('llmModel').value.trim()}})});
    const r = await api('/api/llm_test', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}', timeout:60000});
    if(r && r.ok){
      msg.textContent = '✓ 连接正常：'+(r.sample||'');
    } else {
      msg.textContent = '✗ ' + friendlyLlmError(r && r.error);
    }
  }catch(e){
    if(e && e.name === 'AbortError'){
      msg.textContent = '✗ AI 联通超时：代理 / 网络未连通，请检查本机代理设置或 LLM 端点是否可达';
    } else {
      msg.textContent = '✗ 请求失败：' + (e && e.message ? e.message : e);
    }
  }
}
window.addEventListener('click', e=>{
  if(e.target.classList.contains('modal-mask')) e.target.classList.remove('show');
});

/* ---------- 战略叠加层 ---------- */
function toggleOverlay(btn){
  const on = btn.dataset.on==='1';
  btn.dataset.on = on ? '0' : '1';
  btn.textContent = on ? '战略叠加：关' : '战略叠加：开';
  btn.classList.toggle('off', on);
  overlayOn = !on;
  saveOverlayPref();
}
function syncOvUI(){
  const btn = $('ovBtn');
  if(!btn) return;
  btn.dataset.on = overlayOn ? '1' : '0';
  btn.textContent = overlayOn ? '战略叠加：开' : '战略叠加：关';
  btn.classList.toggle('off', !overlayOn);
}
function saveOverlayPref(){
  try{
    localStorage.setItem('apanel_overlay', JSON.stringify({on: overlayOn}));
  }catch(e){}
}
/* ---------- 设置保存 ---------- */
async function saveSettings(){
  // 筛选
  const filterData = {
    priceMin: $('filtPriceMin').value ? Number($('filtPriceMin').value) : null,
    priceMax: $('filtPriceMax').value ? Number($('filtPriceMax').value) : null,
    main: $('filtMain').checked,
    chiNext: $('filtChiNext').checked,
    st: $('filtSt').checked,
    mcap: $('filtMcap').value
  };
  try{ localStorage.setItem('apanel_filter', JSON.stringify(filterData)); }catch(e){}

  // LLM 配置（key 留空则后端保留原值）
  try{
    await api('/api/config', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({llm:{enabled:$('llmEnabled').checked, endpoint:$('llmEndpoint').value.trim(), api_key:$('llmKey').value.trim(), model:$('llmModel').value.trim()}})});
  }catch(e){ console.warn('LLM 配置保存失败:', e); }

  closeModal('settingsModal');

  // 立即刷新数据（筛选条件变化后重新请求）
  _picksCache = null;      // ★清缓存，否则 `_picksCache || _loadPicksData()` 会复用旧的未过滤数据
  loadAll();
}

/* ---------- 问题反馈上报（腾讯文档收集表，零服务器 / 零中转）---------- */
let _lastErr = '';
window.addEventListener('error', function (e) {
  try { _lastErr = (e.message || '') + (e.filename ? (' @ ' + e.filename + ':' + e.lineno) : ''); } catch (_) {}
});
let _toastTimer = null;
function toast(msg, ms) {
  let t = document.getElementById('apanelToast');
  if (!t) {
    t = document.createElement('div');
    t.id = 'apanelToast';
    t.style.cssText = 'position:fixed;left:50%;bottom:32px;transform:translateX(-50%);background:#2b3242;color:#fff;padding:10px 16px;border-radius:8px;font-size:13px;z-index:999;box-shadow:0 6px 20px rgba(0,0,0,.2);max-width:84vw;line-height:1.5';
    document.body.appendChild(t);
  }
  t.textContent = msg;
  t.style.display = 'block';
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(function () { t.style.display = 'none'; }, ms || 3200);
}
function _copyText(txt) {
  return new Promise(function (resolve) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(txt).then(function(){ resolve(true); }).catch(function(){ resolve(_fallbackCopy(txt)); });
    } else { resolve(_fallbackCopy(txt)); }
  });
}
function _fallbackCopy(txt) {
  try {
    const ta = document.createElement('textarea');
    ta.value = txt; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    const ok = document.execCommand('copy'); document.body.removeChild(ta);
    return ok;
  } catch (e) { return false; }
}
async function reportIssue() {
  let cfg = window.__apanelCfg;
  if (!cfg) { try { cfg = await api('/api/config'); window.__apanelCfg = cfg; } catch (e) { cfg = {}; } }
  const url = (cfg && cfg.issue_form_url) || 'https://docs.qq.com/form/page/DY3ZIc1RWR0tjTWxJ';
  const now = new Date();
  const pad = function (n) { return String(n).padStart(2, '0'); };
  const ts = now.getFullYear() + '-' + pad(now.getMonth() + 1) + '-' + pad(now.getDate()) + ' ' + pad(now.getHours()) + ':' + pad(now.getMinutes()) + ':' + pad(now.getSeconds());
  const ua = navigator.userAgent || '';
  let os = '未知', browser = '未知';
  if (/Windows/.test(ua)) os = 'Windows'; else if (/Mac/.test(ua)) os = 'macOS'; else if (/Linux/.test(ua)) os = 'Linux'; else if (/Android/.test(ua)) os = 'Android'; else if (/iPhone|iPad/.test(ua)) os = 'iOS';
  if (/Edg\//.test(ua)) browser = 'Edge'; else if (/Chrome\//.test(ua)) browser = 'Chrome'; else if (/Firefox\//.test(ua)) browser = 'Firefox'; else if (/Safari\//.test(ua)) browser = 'Safari';
  let cur = '-';
  try { const f = document.getElementById('previewFrame'); if (f && f.src && /code=/.test(f.src)) { const m = f.src.match(/code=([0-9]{6})/); if (m) cur = m[1]; } } catch (_) {}
  const block =
    '【apanel 问题反馈】\n' +
    '版本：' + APP_VERSION + '\n' +
    '时间：' + ts + '\n' +
    '系统：' + os + ' / ' + browser + '\n' +
    '当前个股：' + cur + '\n' +
    '最近报错：' + (_lastErr || '无') + '\n' +
    '--------------------------------\n' +
    '（请在下方描述你遇到的问题，以上为自动诊断信息）';
  const ok = await _copyText(block);
  window.open(url, '_blank');
  toast(ok ? '已复制诊断信息，请在打开的表单里粘贴并提交，谢谢～' : '已打开反馈表单，请手动复制诊断信息后粘贴提交', 4200);
}
