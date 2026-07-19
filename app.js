const $ = (q) => document.querySelector(q);
const $$ = (q) => Array.from(document.querySelectorAll(q));
const tokenKey = 'authToken';
let isAdmin = false;
let currentUsername = '';
// Default to PST if not set
let userTimezone = 'America/Los_Angeles';
let marketLiveTimer = null;
let marketRangePreference = 'MKT';
const MARKET_LIVE_REFRESH_MS = 58000;
const MARKET_SERIES_REFRESH_MS = 60000;
let marketEmbedInFlight = false;
let marketSeriesCache = null;
let marketSeriesLastRefreshMs = 0;
let userStatsHasData = false;
let userStatsLastRefreshMs = 0;
let userStatsAccountName = null;
let userStatsRequestId = 0;
const USER_STATS_CACHE_MS = 15000;

// The modern workspace is the sole supported interface; the legacy theme has
// no conditional stylesheet or per-browser toggle.
try { localStorage.removeItem('volarithm-visual-theme'); } catch (_) {}

// Close both dropdown menus (hamburger and user) and reset caret state
function closeMenus(){
    const userDrop = document.getElementById('user-menu-drop');
    const userWrap = document.getElementById('user-menu');
    const ham = document.getElementById('nav-hamburger');
    if (userDrop) userDrop.classList.add('hidden');
    if (userWrap) userWrap.classList.remove('open');
    if (ham) ham.classList.add('hidden');
}

// Toasts
function showToast(message, kind='success', ms=2400){
    const root = document.getElementById('toast-root');
    if (!root) { console.log(message); return; }
    const existing = Array.from(root.querySelectorAll('.toast'));
    const maxToasts = 3;
    while (existing.length >= maxToasts){
        const oldest = existing.shift();
        try { oldest.remove(); } catch(_) {}
    }
    const t = document.createElement('div');
    t.className = `toast toast-${kind}`;
    t.textContent = message;
    root.appendChild(t);
    const remove = ()=>{
        t.style.animation = 'toast-out .18s ease forwards';
        setTimeout(()=> t.remove(), 180);
    };
    setTimeout(remove, ms);
    t.addEventListener('click', remove);
}

// Timezone helpers
function tzParts(tz, date){
    const fmt = new Intl.DateTimeFormat('en-US', { timeZone: tz, hour12:false, year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', second:'2-digit' });
    const parts = fmt.formatToParts(date).reduce((acc, p)=>{ acc[p.type]=p.value; return acc; }, {});
    return { year:+parts.year, month:+parts.month, day:+parts.day, hour:+parts.hour, minute:+parts.minute, second:+parts.second };
}
function tzOffsetMs(tz, date){
    // Offset between the same instant represented in tz vs UTC
    const p = tzParts(tz, date);
    const asLocal = Date.UTC(p.year, p.month-1, p.day, p.hour, p.minute, p.second);
    return asLocal - date.getTime();
}
function utcMsForWall(tz, year, monthIndex, day, hour=0, minute=0, second=0){
    // Find the UTC ms corresponding to given wall time in tz
    let utc = Date.UTC(year, monthIndex, day, hour, minute, second, 0);
    let off = tzOffsetMs(tz, new Date(utc));
    utc -= off;
    const off2 = tzOffsetMs(tz, new Date(utc));
    if (off2 !== off) utc -= (off2 - off);
    return utc;
}
function nineAmUtcToday(tz){
    const p = tzParts(tz, new Date());
    return utcMsForWall(tz, p.year, p.month-1, p.day, 9, 0, 0);
}
function nineAmUtcForDay(tz, dayStr){
    // dayStr: YYYY-MM-DD
    const [y,m,d] = dayStr.split('-').map(n=>+n);
    if (!y || !m || !d) return nineAmUtcToday(tz);
    return utcMsForWall(tz, y, m-1, d, 9, 0, 0);
}

function setToken(t) {
    if (t) localStorage.setItem(tokenKey, t); else localStorage.removeItem(tokenKey);
}
function getToken() { return localStorage.getItem(tokenKey); }
function authHeaders() { const t=getToken(); return t? { 'Authorization': 'Bearer '+t } : {}; }
function isAuthed(){ return !!getToken(); }
function doLogout(){ setToken(null); try{ history.replaceState({},'', '/'); }catch(_){} location.reload(); }
function show(page) {
    const target = $(page);
    $$('.page').forEach(el => {
        const isTarget = !!target && el === target;
        if (isTarget) {
            el.classList.add('active');
        } else {
            el.classList.remove('active');
        }
        el.removeAttribute('data-loading');
    });
}
// Simple routers for both path and hash (support deep links and older hashes)
function routeFromLocation(){
    const p = (location.pathname || '/').toLowerCase();
    if (!(p.startsWith('/user') || p.startsWith('/home'))) stopMarketLiveUpdates();
    if (p.startsWith('/admin')) { if (isAuthed() && isAdmin){ show('#page-admin'); loadAdmin(); } else { show('#page-home'); } closeMenus(); return; }
    if (p.startsWith('/profile')) { if (isAuthed()){ loadProfile(); show('#page-profile'); } else { show('#page-home'); } closeMenus(); return; }
    if (p.startsWith('/user') || p.startsWith('/home')) { if (isAuthed()){ show('#page-user'); loadUserData({preserve:true}); } else { show('#page-home'); } closeMenus(); return; }
    if (p.startsWith('/about')) { show('#page-home'); closeMenus(); return; }
    // Default routing: if authenticated, go to user page; otherwise home
    if (isAuthed()) {
        history.replaceState(null, '', '/user');
        show('#page-user');
        loadUserData({preserve:true});
    } else {
        show('#page-home');
    }
    closeMenus();
}
function routeFromHash(){
    const h = (location.hash || '').toLowerCase();
    if (!h) return false;
    if (!(h.startsWith('#/user') || h.startsWith('#/home'))) stopMarketLiveUpdates();
    if (h.startsWith('#/admin')) { if (isAuthed() && isAdmin){ show('#page-admin'); loadAdmin(); } else { show('#page-home'); } return true; }
    if (h.startsWith('#/profile')) { if (isAuthed()){ loadProfile(); show('#page-profile'); } else { show('#page-home'); } return true; }
    if (h.startsWith('#/user') || h.startsWith('#/home')) { if (isAuthed()){ show('#page-user'); loadUserData({preserve:true}); } else { show('#page-home'); } return true; }
    if (h.startsWith('#/about')) { show('#page-home'); return true; }
    return false;
}
window.addEventListener('popstate', ()=>{ routeFromLocation(); closeMenus(); });
window.addEventListener('hashchange', ()=>{ if (!routeFromHash()) routeFromLocation(); closeMenus(); });

// Navigation (SPA): prevent full loads and push state
$('#nav-home').addEventListener('click', (e)=>{ e.preventDefault(); try{ history.pushState({},'', '/about'); }catch(_){} routeFromLocation(); });
$('#nav-user').addEventListener('click', (e)=>{ e.preventDefault(); try{ history.pushState({},'', '/home'); }catch(_){} routeFromLocation(); });
$('#nav-admin').addEventListener('click', (e)=>{ e.preventDefault(); try{ history.pushState({},'', '/admin'); }catch(_){} routeFromLocation(); });

// Auth dialog
const dlg = $('#dlg-auth');
function openAuth(mode) {
    console.log('openAuth called with mode:', mode);
    $('#auth-title').textContent = mode === 'signup' ? 'Sign Up' : 'Sign In';
    $('#auth-hint').textContent = '';
    // reset fields for a clean start
    $('#auth-username').value = '';
    $('#auth-password').value = '';
    $('#pw-req').style.display = 'none';
    $$('#pw-req li').forEach(li=>li.classList.remove('ok'));
    // submit button label
    $('#auth-submit').textContent = (mode === 'signup') ? 'Sign Up' : 'Login';
    // set password autocomplete best-practice for mode
    const pw = $('#auth-password');
    pw.setAttribute('autocomplete', mode === 'signup' ? 'new-password' : 'current-password');
    dlg.dataset.mode = mode;
    console.log('About to show dialog');
    dlg.showModal();
    console.log('Dialog should be visible now');
}
$('#cta-login').onclick = ()=> {
console.log('Login button clicked');
openAuth('login');
};
$('#cta-signup').onclick = ()=> {
console.log('Signup button clicked');
openAuth('signup');
};
$('#cta-signup-2')?.addEventListener('click', ()=>{
console.log('Signup button 2 clicked');
openAuth('signup');
});
$('#cta-learn-more')?.addEventListener('click', ()=>{
console.log('Learn more clicked');
const el = document.querySelector('.card h3');
if (el) el.scrollIntoView({ behavior: 'smooth' });
});

// User-menu dropdown behavior
(function initUserMenu(){
const btn = $('#user-menu-btn');
const drop = $('#user-menu-drop');
if (!btn || !drop) return;
btn.addEventListener('click', (e)=>{
    e.stopPropagation();
    drop.classList.toggle('hidden');
    // Toggle open class for caret rotation
    const wrap = document.getElementById('user-menu');
    if (wrap){ wrap.classList.toggle('open', !drop.classList.contains('hidden')); }
});
document.addEventListener('click', (e)=>{
    if (!drop.classList.contains('hidden')){
        if (!drop.contains(e.target) && e.target !== btn){
            drop.classList.add('hidden');
            const wrap = document.getElementById('user-menu');
            if (wrap){ wrap.classList.remove('open'); }
        }
    }
});
$('#menu-profile')?.addEventListener('click', ()=>{
    try{ history.pushState({},'', '/profile'); }catch(_){}
    routeFromLocation();
    drop.classList.add('hidden');
    const wrap = document.getElementById('user-menu');
    if (wrap){ wrap.classList.remove('open'); }
});
$('#menu-logout')?.addEventListener('click', ()=>{
    doLogout();
    drop.classList.add('hidden');
    const wrap = document.getElementById('user-menu');
    if (wrap){ wrap.classList.remove('open'); }
});
})();

// Hamburger menu behavior (small screens)
(function initHamburger(){
const btn = document.getElementById('menu-btn');
const drop = document.getElementById('nav-hamburger');
const linkAbout = document.getElementById('menu-about');
const linkHome = document.getElementById('menu-home');
const linkAdmin = document.getElementById('menu-admin');
if (!btn || !drop) return;
function close(){ drop.classList.add('hidden'); }
btn.addEventListener('click', (e)=>{ e.stopPropagation(); drop.classList.toggle('hidden'); });
document.addEventListener('click', (e)=>{ if (!drop.classList.contains('hidden') && !drop.contains(e.target) && e.target!==btn) close(); });
linkAbout?.addEventListener('click', (e)=>{ e.preventDefault?.(); try{ history.pushState({},'', '/about'); }catch(_){} close(); routeFromLocation(); });
linkHome?.addEventListener('click', (e)=>{ e.preventDefault?.(); try{ history.pushState({},'', '/home'); }catch(_){} close(); routeFromLocation(); });
linkAdmin?.addEventListener('click', (e)=>{ e.preventDefault?.(); try{ history.pushState({},'', '/admin'); }catch(_){} close(); routeFromLocation(); });

// Reflect nav visibility in hamburger - sync Home and Admin links
const navUser = document.getElementById('nav-user');
const navAdmin = document.getElementById('nav-admin');
const syncVis = ()=>{
    const homeHidden = navUser?.classList.contains('hidden');
    const adminHidden = navAdmin?.classList.contains('hidden');
    linkHome?.classList.toggle('hidden', !!homeHidden);
    linkAdmin?.classList.toggle('hidden', !!adminHidden);

    // Hide hamburger menu entirely if not authenticated (only About link would be visible)
    const shouldHideMenu = !isAuthed();
    btn.style.display = shouldHideMenu ? 'none' : 'block';
};

if (navUser){ const obsUser = new MutationObserver(syncVis); obsUser.observe(navUser, {attributes:true, attributeFilter:['class']}); }
if (navAdmin){ const obsAdmin = new MutationObserver(syncVis); obsAdmin.observe(navAdmin, {attributes:true, attributeFilter:['class']}); }
syncVis(); // Initial sync
})();

// Cancel closes dialog without triggering validation
$('#auth-cancel').onclick = ()=> { try{ dlg.close(); } catch(_){} };
// Support Esc key to close
dlg.addEventListener('cancel', (e)=>{ e.preventDefault(); try{ dlg.close(); }catch(_){} });
// Close only when clicking the backdrop (not while selecting inside the form)
// Robust backdrop close: only when the pointer started on the backdrop
let backdropDown = false;
dlg.addEventListener('mousedown', (e)=>{ backdropDown = (e.target === dlg); });
dlg.addEventListener('click', (e)=>{ if (e.target === dlg && backdropDown) { try{ dlg.close(); }catch(_){} } backdropDown=false; });

// Save credentials in supported browsers (Credential Management API) and fallback for iOS/Safari
async function storeLoginForAutofill(username, password){
try{
    if ('credentials' in navigator && window.PasswordCredential) {
        // Requires secure context (HTTPS) for most browsers
        const cred = new window.PasswordCredential({ id: username, name: username, password });
        await navigator.credentials.store(cred).catch(()=>{});
    }
}catch{}
// Fallback: submit a hidden form to /api/login in a hidden iframe to trigger save-password UX
try{
    let iframe = document.getElementById('pw-store-frame');
    if (!iframe){
        iframe = document.createElement('iframe');
        iframe.name = 'pw-store-frame';
        iframe.id = 'pw-store-frame';
        iframe.style.display = 'none';
        document.body.appendChild(iframe);
    }
    const form = document.createElement('form');
    form.action = '/api/login';
    form.method = 'POST';
    form.target = 'pw-store-frame';
    form.style.display = 'none';
    const u = document.createElement('input'); u.name='username'; u.value = username;
    const p = document.createElement('input'); p.name='password'; p.value = password; p.type='password';
    form.appendChild(u); form.appendChild(p);
    document.body.appendChild(form);
    form.submit();
    setTimeout(()=>{ form.remove(); }, 1500);
}catch{}
}

$('#auth-submit').onclick = async (e)=>{
    e.preventDefault();
    const u = $('#auth-username').value.trim();
    const p = $('#auth-password').value;
    try {
        if (dlg.dataset.mode === 'signup') {
            // client-side strong password check mirrors server rule
            const strong = /^(?=.*[A-Za-z])(?=.*\d).{10,20}$/.test(p);
            if (!strong) {
                // Show requirements, place focus, and use native validity bubble
                $('#pw-req').style.display = 'block';
                const pw = $('#auth-password');
                pw.setCustomValidity('Password must be 10-20 characters and include letters and numbers.');
                pw.reportValidity();
                pw.focus();
                return;
            } else {
                $('#auth-password').setCustomValidity('');
            }
            const r = await fetch('/api/signup', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({username:u,password:p,is_admin:false})});
            if (!r.ok) throw new Error((await r.json()).detail || 'signup failed');
            // Prompt the browser to save these credentials now
            storeLoginForAutofill(u, p);
            openAuth('login');
            return;
        } else {
            const body = new URLSearchParams({username:u, password:p});
            const r = await fetch('/api/login', {method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'}, body});
            if (!r.ok) throw new Error((await r.json()).detail || 'login failed');
            const js = await r.json();
            setToken(js.access_token);
            dlg.close();
            // Ask browser/OS to save the credentials
            storeLoginForAutofill(u, p);
            await postLogin();
        }
    } catch (err){ $('#auth-hint').textContent = err.message; }
};

// Auth password eye toggle
(function(){
const btn = document.getElementById('auth-password-toggle');
const inp = document.getElementById('auth-password');
if (btn && inp){
    const sync = ()=>{
        const isPw = inp.getAttribute('type') === 'password';
        btn.classList.toggle('is-revealed', !isPw);
        btn.title = isPw ? 'Show password' : 'Hide password';
        btn.setAttribute('aria-label', btn.title);
    };
    sync();
    btn.addEventListener('click', ()=>{
        const isPw = inp.getAttribute('type') === 'password';
        inp.setAttribute('type', isPw ? 'text' : 'password');
        sync();
    });
}
})();

// Dynamic password requirements visibility and checks
const pwInput = $('#auth-password');
pwInput.addEventListener('input', ()=>{
const v = pwInput.value || '';
const isSignup = (dlg.dataset.mode === 'signup');
const hasLen = v.length >= 10;
const hasMax = v.length <= 20;
const hasLet = /[A-Za-z]/.test(v);
const hasDig = /\d/.test(v);
// Only show requirements when signing up
$('#pw-req').style.display = (isSignup && v.length) ? 'block' : 'none';
if (isSignup){
    const set = (sel, ok)=>{ const el = $(`#pw-req li[data-rule="${sel}"]`); if (el) el.classList.toggle('ok', !!ok); };
    set('len', hasLen); set('max', hasMax); set('letters', hasLet); set('digits', hasDig);
    // Clear or set custom validity live (signup only)
    if (hasLen && hasMax && hasLet && hasDig) { pwInput.setCustomValidity(''); }
    else { pwInput.setCustomValidity('Password must be 10-20 characters and include letters and numbers.'); }
} else {
    // No custom validity for login mode
    pwInput.setCustomValidity('');
}
});

async function postLogin(){
    // Who am I
    const r = await fetch('/api/me', {headers: authHeaders()});
    if (!r.ok) { setToken(null); return; }
    const me = await r.json();
    isAdmin = !!me.is_admin;
    currentUsername = me.username || '';
    document.body.classList.add('authed');
    const uname = currentUsername || 'user';
    $('#user-menu-name').textContent = uname;
    $('#user-menu').classList.remove('hidden');
    // The modern About page keeps its strategy visual while the classic layout
    // still hides the sign-up panel through its authenticated CSS rule.
    $('#nav-user').classList.remove('hidden');
    if (isAdmin) $('#nav-admin').classList.remove('hidden');

    // Check bot status for non-admin users
    if (!isAdmin) {
        try {
            const botStatus = await fetch('/api/bot/status', {headers: authHeaders()});
            if (botStatus.ok) {
                const status = await botStatus.json();
                if (!status.running) {
                    // Redirect non-admin users to bot down page
                    show('#page-bot-down');
                    return;
                }
            }
        } catch (error) {
            console.error('Failed to check bot status:', error);
        }
    }

    // Fetch profile to set timezone
    try{
        const pr = await fetch('/api/user/profile', {headers: authHeaders()});
        if (pr.ok){ const p = await pr.json(); if (p.timezone) userTimezone = p.timezone; }
    }catch{}

    // After login, redirect root to user home page, but preserve intentional about page visits
    if (location.pathname === '/' || location.pathname === '') {
        try { history.replaceState({}, '', '/home'); } catch(e) { location.pathname = '/home'; }
    }

    // Show the correct page based on the new path
    routeFromLocation();

    if (isAdmin) loadAdmin();
}

// Minimal admin loader (fetches stats and populates prior-days table)
async function loadAdmin(){
    try{
        const r = await fetch('/api/history/daily', {headers: authHeaders()});
        const js = await r.json().catch(()=>({days:[]}));
        const body = $('#history-body'); if (!body) return; body.innerHTML='';
        const days = Array.isArray(js.days)? js.days: [];
        for (const d of days){
            const tr = document.createElement('tr');
            const td1 = document.createElement('td'); td1.textContent = d.date || '';
            const td2 = document.createElement('td');

            // Format P&L with color coding
            const pnl = Number(d.pnl_pct) || 0;
            td2.textContent = pnl.toFixed(2) + '%';
            if (pnl > 0) {
                td2.style.color = '#4caf50'; // Green for profit
            } else if (pnl < 0) {
                td2.style.color = '#f44336'; // Red for loss
            }

            tr.append(td1, td2); body.appendChild(tr);
        }
    }catch(e){ console.warn('loadAdmin failed', e); }
    initAdminControls(); // Initialize admin controls
    // Wire refresh-stats button once
    const rb = document.getElementById('refresh-stats-btn');
    if (rb && !rb.dataset.wired) {
        rb.dataset.wired = '1';
        rb.addEventListener('click', async ()=>{
            try{
                const r = await fetch('/api/stats/refresh', {headers: authHeaders()});
                if (!r.ok) throw new Error('refresh failed');
                const js = await r.json().catch(()=>({}));
                showToast('Stats refreshed.', 'success');
                // Also update the About page figures now
                try { await loadStats(); } catch {}
            }catch(err){ showToast(err.message||'Failed to refresh stats', 'error'); }
        });
    }
    await loadAdminTradingAccounts();
}

function initAdminControls() {
    // Idempotent init: avoid attaching duplicate listeners on re-entry
    const adminRoot = document.getElementById('page-admin');
    if (adminRoot && adminRoot.dataset.inited === '1') return;
    if (adminRoot) adminRoot.dataset.inited = '1';
    // Emergency Shutdown
    const shutdownBtn = $('#shutdown-bot-btn');
    const emergencyDlg = $('#dlg-emergency');
    if (shutdownBtn && emergencyDlg) {
        shutdownBtn.addEventListener('click', () => emergencyDlg.showModal());
        $('#emergency-cancel').addEventListener('click', () => emergencyDlg.close());
        $('#emergency-confirm').addEventListener('click', async () => {
            try {
                const r = await fetch('/api/bot/shutdown', { method: 'POST', headers: authHeaders() });
                if (!r.ok) throw new Error((await r.json()).detail || 'Failed to shutdown bot');
                showToast('Bot shutdown initiated.', 'success');
                emergencyDlg.close();
                const indicator = $('#bot-status-indicator');
                if(indicator) {
                    indicator.textContent = 'Shutdown';
                }
            } catch (err) {
                showToast(err.message, 'error');
            }
        });
    }

    // Segmented controls
    function initSegControl(groupId) {
        const group = $(groupId);
        if (!group) return;
        group.addEventListener('click', (e) => {
            if (e.target.classList.contains('seg-btn')) {
                $$(`${groupId} .seg-btn`).forEach(btn => btn.classList.remove('active'));
                e.target.classList.add('active');
            }
        });
    }
    initSegControl('#priority-group');
    initSegControl('#stake-group');

    // Helper to set active button by value
    function setSegActive(groupId, value){
        const group = $(groupId); if (!group) return;
        const btns = $$(groupId + ' .seg-btn');
        btns.forEach(b => b.classList.remove('active'));
        const match = btns.find(b => String(b.dataset.value) === String(value));
        if (match) match.classList.add('active');
    }

    // Fetch current control state and reflect in UI (best-effort)
    (async () => {
        try {
            const cr = await fetch('/api/control', { headers: authHeaders() });
            if (cr.ok) {
                const cj = await cr.json();
                const pr = (cj.priority_side || '').toString().toUpperCase();
                // Use '' for default in UI group
                const prVal = (pr === 'UP' || pr === 'DOWN') ? pr : '';
                setSegActive('#priority-group', prVal);
                // Stake multiplier: coerce to canonical string values we expose
                const sm = (cj.stake_multiplier != null) ? String(cj.stake_multiplier) : '1';
                const allowed = new Set(['0.5','1','2']);
                setSegActive('#stake-group', allowed.has(sm) ? sm : '1');
            }
        } catch {}
    })();

    // Save Priority & Stake
    const saveControlBtn = $('#save-control');
    if (saveControlBtn) {
        saveControlBtn.addEventListener('click', async () => {
            // Determine selected values
            const priorityBtn = $('#priority-group .seg-btn.active');
            const stakeBtn = $('#stake-group .seg-btn.active');
            const priorityValue = priorityBtn ? priorityBtn.dataset.value : undefined;
            const stakeValue = stakeBtn ? stakeBtn.dataset.value : undefined;
            try {
                // Build payload strictly: include only fields the server expects, avoid empty strings
                const body = {};
                if (typeof priorityValue !== 'undefined') {
                    // Backend expects 'priority_side'; send '' to clear to default
                    body.priority_side = (priorityValue === '' ? '' : priorityValue);
                }
                if (typeof stakeValue !== 'undefined') {
                    const n = Number(stakeValue);
                    if (!Number.isNaN(n)) body.stake_multiplier = n;
                }
                // If no changes selected, still send an empty object which the API should accept; otherwise, it's a no-op
                const r = await fetch('/api/control', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', ...authHeaders() },
                    body: JSON.stringify(body)
                });
                if (!r.ok) {
                    const err = await r.json().catch(()=>({detail: 'Failed to save settings'}));
                    throw new Error(err.detail);
                }
                showToast('Bot control settings saved.', 'success');
            } catch (err) {
                showToast(err.message, 'error');
            }
        }, { once: false });
    }

    // Bot Commands
    const cmdToolbar = $('#page-admin .toolbar');
    if (cmdToolbar) {
        cmdToolbar.addEventListener('click', async (e) => {
            if (e.target.dataset.cmd) {
                const cmd = e.target.dataset.cmd;
                try {
                    // Backend expects /api/command with 'cmd' param; send as query
                    const r = await fetch(`/api/command?cmd=${encodeURIComponent(cmd)}`, {
                        method: 'POST',
                        headers: { ...authHeaders() }
                    });
                    if (!r.ok) {
                        const err = await r.json().catch(()=>({detail: `Command '${cmd}' failed`}));
                        throw new Error(err.detail);
                    }
                    const result = await r.json();
                    showToast(`Command '${cmd}' executed successfully.`, 'success');
                } catch (err) {
                    showToast(err.message, 'error');
                }
            }
        });
    }
}

function setMarketHostLoading(host){
    host.dataset.marketLoading = '1';
    host.dataset.marketLoadingStart = String(Date.now());
    if (!document.getElementById('market-loader-inline-keyframes')){
        const st = document.createElement('style');
        st.id = 'market-loader-inline-keyframes';
        st.textContent = `
            @keyframes marketSkelShift {
                0% { background-position: 200% 0; }
                100% { background-position: -200% 0; }
            }
        `;
        document.head.appendChild(st);
    }
    host.innerHTML = `
        <div class="market-loading-skeleton" aria-hidden="true" style="position:absolute;inset:0;border-radius:8px;background:linear-gradient(90deg, rgba(255,255,255,.06) 25%, rgba(255,255,255,.16) 50%, rgba(255,255,255,.06) 75%);background-size:200% 100%;animation:marketSkelShift 1.15s linear infinite;z-index:0;"></div>
        <div class="market-skel-card" aria-hidden="true" style="position:absolute;inset:12px 14px;display:flex;flex-direction:column;gap:10px;z-index:1;">
            <div class="market-skel-row" style="display:flex;justify-content:space-between;align-items:center;gap:10px;">
                <div class="market-skel-pill w-40" style="height:14px;width:40%;border-radius:8px;background:linear-gradient(90deg, rgba(255,255,255,.18) 20%, rgba(255,255,255,.36) 50%, rgba(255,255,255,.18) 80%);background-size:220% 100%;animation:marketSkelShift 1.15s ease-in-out infinite;"></div>
                <div class="market-skel-pill w-12" style="height:14px;width:12%;border-radius:8px;background:linear-gradient(90deg, rgba(255,255,255,.18) 20%, rgba(255,255,255,.36) 50%, rgba(255,255,255,.18) 80%);background-size:220% 100%;animation:marketSkelShift 1.15s ease-in-out infinite;"></div>
            </div>
            <div class="market-skel-pill w-28" style="height:14px;width:28%;border-radius:8px;background:linear-gradient(90deg, rgba(255,255,255,.18) 20%, rgba(255,255,255,.36) 50%, rgba(255,255,255,.18) 80%);background-size:220% 100%;animation:marketSkelShift 1.15s ease-in-out infinite;"></div>
            <div class="market-skel-graph" style="flex:1;min-height:180px;border-radius:10px;background:linear-gradient(90deg, rgba(255,255,255,.12) 20%, rgba(255,255,255,.26) 50%, rgba(255,255,255,.12) 80%);background-size:220% 100%;animation:marketSkelShift 1.15s ease-in-out infinite;"></div>
            <div class="market-skel-row" style="display:flex;justify-content:space-between;align-items:center;gap:10px;">
                <div class="market-skel-pill w-16" style="height:14px;width:16%;border-radius:8px;background:linear-gradient(90deg, rgba(255,255,255,.18) 20%, rgba(255,255,255,.36) 50%, rgba(255,255,255,.18) 80%);background-size:220% 100%;animation:marketSkelShift 1.15s ease-in-out infinite;"></div>
                <div class="market-skel-pill w-16" style="height:14px;width:16%;border-radius:8px;background:linear-gradient(90deg, rgba(255,255,255,.18) 20%, rgba(255,255,255,.36) 50%, rgba(255,255,255,.18) 80%);background-size:220% 100%;animation:marketSkelShift 1.15s ease-in-out infinite;"></div>
            </div>
        </div>
        <div class="market-loading-text" style="position:relative;z-index:2;color:#c6d8f3;font-size:13px;font-weight:600;text-shadow:0 1px 0 rgba(0,0,0,.25);pointer-events:none;">Loading market...</div>
    `;
}

async function ensureMarketLoaderVisible(host, showLoading, minMs = 550){
    if (!showLoading || !host) return;
    const started = Number(host.dataset.marketLoadingStart || 0);
    if (!Number.isFinite(started) || started <= 0) return;
    const elapsed = Date.now() - started;
    const wait = Math.max(0, minMs - elapsed);
    if (wait > 0){
        await new Promise((resolve)=> setTimeout(resolve, wait));
    }
}

function clearMarketLoadingState(host){
    if (!host) return;
    delete host.dataset.marketLoading;
    delete host.dataset.marketLoadingStart;
}

function withLatestCurrentPoint(series, cur){
    const points = Array.isArray(series?.points) ? series.points.slice() : [];
    const upRaw = toFiniteNumber(cur?.up?.mid ?? cur?.up?.ask ?? cur?.up?.bid);
    const downRaw = toFiniteNumber(cur?.down?.mid ?? cur?.down?.ask ?? cur?.down?.bid);
    let up = upRaw;
    let down = downRaw;
    if (up !== null && up > 1) up = up / 100;
    if (down !== null && down > 1) down = down / 100;
    // If one side is missing, derive complement from the other.
    if (up === null && down !== null) up = 1 - down;
    if (down === null && up !== null) down = 1 - up;
    // If still missing, don't inject a fake 0% point.
    if (up === null || down === null) return { points };
    points.push({
        ts: new Date().toISOString(),
        up,
        down,
    });
    return { points };
}

function toFiniteNumber(v){
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
}

function normalizedMid(cur, side){
    const raw = toFiniteNumber(cur?.[side]?.mid ?? cur?.[side]?.ask ?? cur?.[side]?.bid);
    if (raw === null) return null;
    if (raw > 1) return raw / 100;
    return raw;
}

function currentLooksUsable(cur){
    const up = normalizedMid(cur, 'up');
    const down = normalizedMid(cur, 'down');
    return (up !== null && up > 0) || (down !== null && down > 0);
}

async function fetchPolymarketEventBySlug(slug) {
    const ctl = new AbortController();
    const timeout = setTimeout(()=> ctl.abort(), 8000);
    let res;
    try {
        // Route through backend to avoid browser CORS/rate-limit edge cases.
        res = await fetch(`/api/polymarket/current-by-slug?slug=${encodeURIComponent(slug)}`, {
            signal: ctl.signal,
            headers: authHeaders(),
        });
    } finally {
        clearTimeout(timeout);
    }
    if (!res.ok) throw new Error(`Gamma current fetch failed: ${res.status}`);
    return res.json();
}

function parseJsonArrayMaybe(v){
    if (Array.isArray(v)) return v;
    if (typeof v !== 'string') return null;
    try {
        const parsed = JSON.parse(v);
        return Array.isArray(parsed) ? parsed : null;
    } catch (_) {
        return null;
    }
}

function findGammaMarketBySlug(eventPayload, slug){
    if (!eventPayload || typeof eventPayload !== 'object') return null;
    const list = Array.isArray(eventPayload?.markets) ? eventPayload.markets : [];
    if (!list.length) return null;
    const exact = list.find((m)=> String(m?.slug || '').trim() === String(slug || '').trim());
    return exact || list[0] || null;
}

function currentFromGammaEvent(eventPayload, slug){
    // Backend-normalized preferred payload.
    if (eventPayload?.current?.up?.mid != null) {
        return eventPayload.current;
    }
    const market = findGammaMarketBySlug(eventPayload, slug) || eventPayload?.market;
    if (!market) return null;

    const outcomes = parseJsonArrayMaybe(market?.outcomes) || [];
    const prices = parseJsonArrayMaybe(market?.outcomePrices) || [];
    if (prices.length < 2){
        const bid = toFiniteNumber(market?.bestBid);
        const ask = toFiniteNumber(market?.bestAsk);
        const last = toFiniteNumber(market?.lastTradePrice);
        const mid = (bid !== null && ask !== null) ? ((bid + ask) / 2) : (last ?? ask ?? bid);
        if (mid === null) return null;
        const upMid = mid > 1 ? (mid / 100) : mid;
        return { up: { mid: upMid }, down: { mid: 1 - upMid } };
    }

    const labels = outcomes.map((x)=> String(x || '').toLowerCase());
    let upIdx = labels.findIndex((x)=> x.includes('up'));
    if (upIdx < 0) upIdx = 0;
    const downIdx = upIdx === 0 ? 1 : 0;
    const up = toFiniteNumber(prices[upIdx]);
    const down = toFiniteNumber(prices[downIdx]);
    if (up === null || down === null) return null;
    return {
        up: { mid: up > 1 ? up / 100 : up },
        down: { mid: down > 1 ? down / 100 : down },
    };
}

function applyGammaEventToActive(active, gammaEvent, slug){
    const market = findGammaMarketBySlug(gammaEvent, slug);
    if (!market) return active;
    return {
        ...active,
        market: {
            ...(active?.market || {}),
            slug: market?.slug || active?.market?.slug || slug,
            title: market?.question || active?.market?.title,
            question: market?.question || active?.market?.question,
        },
    };
}

function preferBestCurrent(primaryCur, fallbackCur){
    const pUp = normalizedMid(primaryCur, 'up');
    const fUp = normalizedMid(fallbackCur, 'up');
    // If primary is missing or pinned at 0 while fallback has a real value, use fallback.
    if (fUp !== null && fUp > 0 && (pUp === null || pUp <= 0)) return fallbackCur;
    if (currentLooksUsable(primaryCur)) return primaryCur;
    if (currentLooksUsable(fallbackCur)) return fallbackCur;
    return primaryCur || fallbackCur || {};
}

function normalizeOutcomeLabel(v, i){
    const s = String(v || '').trim();
    if (!s) return i === 0 ? 'Yes' : (i === 1 ? 'No' : `Outcome ${i+1}`);
    return s;
}

function findPredictionOutcomes(payload){
    const seen = [];
    const queue = [payload];
    while (queue.length){
        const cur = queue.shift();
        if (!cur || typeof cur !== 'object') continue;
        if (Array.isArray(cur)){
            cur.forEach(v => queue.push(v));
            continue;
        }
        const keys = Object.keys(cur);
        const hasLabel = ('label' in cur) || ('name' in cur) || ('title' in cur);
        const hasProb = ('probability' in cur) || ('price' in cur) || ('lastPrice' in cur) || ('value' in cur);
        if (hasLabel && hasProb){
            const label = cur.label || cur.name || cur.title;
            const raw = cur.probability ?? cur.price ?? cur.lastPrice ?? cur.value;
            const num = Number(raw);
            if (Number.isFinite(num)){
                const pct = num > 1 ? num : (num * 100);
                seen.push({ label: String(label || ''), pct: Math.max(0, Math.min(100, pct)) });
            }
        }
        keys.forEach(k => queue.push(cur[k]));
    }
    const byLabel = new Map();
    for (const o of seen){
        const key = o.label.trim().toLowerCase();
        if (!key) continue;
        if (!byLabel.has(key)) byLabel.set(key, o);
    }
    return Array.from(byLabel.values()).slice(0, 2);
}

function renderPredictionMarketCard(host, slug, payload){
    const root = (payload && typeof payload === 'object') ? payload : {};
    const title = String(
        root?.event?.title ||
        root?.event?.name ||
        root?.title ||
        root?.name ||
        slug || 'Today\'s market'
    ).trim();
    const outcomes = findPredictionOutcomes(root);
    const rows = outcomes.length ? outcomes : [{label:'Yes', pct:50},{label:'No', pct:50}];
    const src = `https://polymarket.com/event/${encodeURIComponent(slug)}?utm_source=yahoo_finance&utm_medium=referral`;
    host.innerHTML = `
        <div class="yahoo-market-card">
            <div class="ym-head">
                <div class="ym-title">${title}</div>
                <a class="ym-link" href="${src}" target="_blank" rel="noopener">Powered by Polymarket</a>
            </div>
            <div class="ym-rows">
                ${rows.map((o, i)=>`
                    <div class="ym-row">
                        <div class="ym-row-top">
                            <span>${normalizeOutcomeLabel(o.label, i)}</span>
                            <span>${Number(o.pct).toFixed(1)}%</span>
                        </div>
                        <div class="ym-bar"><span style="width:${Math.max(0, Math.min(100, Number(o.pct)||0))}%"></span></div>
                    </div>
                `).join('')}
            </div>
        </div>
    `;
}

function renderLocalGraphCard(host, slug, active, daySeries, prices, preferredRange){
    const title = String(active?.market?.question || active?.market?.title || slug || "Today's market").trim();
    const marketUrl = `https://polymarket.com/event/${encodeURIComponent(slug)}`;
    const upMid = Number(prices?.up?.mid);
    if (!Number.isFinite(upMid)) {
        throw new Error('Missing or invalid up.mid price');
    }
    const upPct = Math.max(0, Math.min(100, upMid > 1 ? upMid : (upMid * 100)));

    const pts = Array.isArray(daySeries?.points) ? daySeries.points : [];
    const nowMs = Date.now();
    const normalized = pts
        .map((p)=>{
            const ts = String(p?.ts || '');
            const t = Date.parse(ts);
            const upRaw = toFiniteNumber(p?.up);
            const downRaw = toFiniteNumber(p?.down);
            let up = upRaw;
            if (up === null && downRaw !== null){
                const downNorm = downRaw > 1 ? (downRaw / 100) : downRaw;
                up = 1 - downNorm;
            }
            if (up !== null && up > 1) up = up / 100;
            return { ts, up, t };
        })
        .filter((p)=>Number.isFinite(p.up))
        .filter((p)=>Number.isFinite(p.t))
        .filter((p)=>p.t <= (nowMs + 60 * 1000))
        .sort((a,b)=>a.t-b.t);

    const W = 980;
    const H = 228;
    const PAD_T = 12;
    const PAD_B = 26;
    const PAD_L = 8;
    const PAD_R = 58;
    const minY = 0;
    const maxY = 100;
    const n = normalized.length;
    const pctValue = (v)=> {
        const p = Number(v);
        if (!Number.isFinite(p)) return 0;
        return Math.max(0, Math.min(100, p > 1 ? p : (p * 100)));
    };
    const toY = (v)=> {
        const clamped = pctValue(v);
        const t = (clamped - minY) / (maxY - minY);
        return Math.round(PAD_T + (1 - t) * (H - PAD_T - PAD_B));
    };

    function marketStartMs(){
        // Align with backend/Yahoo market-day roll:
        // market date D starts at noon ET on D-1 and runs until noon ET on D.
        const activeDate = String(active?.date || '');
        const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(activeDate);
        const tz = 'America/New_York';
        if (!m){
            // Conservative fallback: derive from "now" using the same noon-ET roll.
            const now = new Date();
            const p = tzParts(tz, now);
            let noon = utcMsForWall(tz, p.year, p.month - 1, p.day, 12, 0, 0);
            if (now.getTime() < noon) noon -= (24 * 60 * 60 * 1000);
            return noon - (24 * 60 * 60 * 1000);
        }
        const year = Number(m[1]);
        const month = Number(m[2]);
        const day = Number(m[3]);
        const marketDayNoon = utcMsForWall(tz, year, month - 1, day, 12, 0, 0);
        return marketDayNoon - (24 * 60 * 60 * 1000);
    }

    function rangeBounds(rangeKey){
        const endMs = Date.now();
        if (rangeKey === 'MKT'){
            return { startMs: marketStartMs(), endMs };
        }
        const rangeMinutes = { '1M': 1, '10M': 10, '30M': 30, '1H': 60, '3H': 180 };
        const mins = rangeMinutes[rangeKey] || 60;
        return { startMs: endMs - (mins * 60 * 1000), endMs };
    }

    function pointsForRange(rangeKey){
        if (!n){
            const b = rangeBounds(rangeKey);
            // Keep a real, inspectable baseline visible while the recorder is
            // warming up or a refresh temporarily returns no historical rows.
            return {
                points: [
                    { ts: new Date(b.startMs).toISOString(), t: b.startMs, up: upPct },
                    { ts: new Date(b.endMs).toISOString(), t: b.endMs, up: upPct },
                ],
                ...b,
            };
        }
        const b = rangeBounds(rangeKey);
        const window = normalized.filter((p)=>p.t >= b.startMs && p.t <= (b.endMs + 60 * 1000));
        if (window.length >= 2) return { points: window, ...b };
        if (window.length === 1) return { points: [window[0], { ...window[0], t: b.endMs, up: upPct }], ...b };
        const last = normalized[normalized.length - 1];
        return last
            ? { points: [{ ...last, t: b.startMs, up: last.up }, { ...last, t: b.endMs, up: upPct }], ...b }
            : { points: [], ...b };
    }

    function labelFor(ts, rangeKey){
        const d = new Date(ts);
        const opts = { timeZone: (userTimezone || 'America/Los_Angeles') };
        if (rangeKey === '3H' || rangeKey === '1H' || rangeKey === '30M' || rangeKey === '10M' || rangeKey === '1M') {
            return d.toLocaleTimeString([], { ...opts, hour:'numeric', minute:'2-digit' });
        }
        return d.toLocaleTimeString([], { ...opts, hour:'numeric' }).replace(':00','');
    }

    function hoverTime(ts){
        return new Date(ts).toLocaleString([], { timeZone: (userTimezone || 'America/Los_Angeles'), month:'short', day:'numeric', hour:'numeric', minute:'2-digit' });
    }

    function chartMarkup(data, rangeKey, bounds){
        const m = data.length;
        const startMs = Number(bounds?.startMs || Date.now() - 3600000);
        const endMs = Math.max(startMs + 1000, Number(bounds?.endMs || Date.now()));
        const xForTs = (ts)=>{
            const t = Math.max(startMs, Math.min(endMs, Number(ts) || startMs));
            const ratio = (t - startMs) / Math.max(1, endMs - startMs);
            return Math.round(PAD_L + ratio * (W - PAD_L - PAD_R));
        };
        const path = m ? data.map((p, i)=>`${i===0?'M':'L'}${xForTs(p.t)},${toY(p.up)}`).join(' ') : '';
        const lastX = m ? xForTs(data[m - 1].t) : xForTs(endMs);
        const lastY = m ? toY(data[m - 1].up) : toY(upPct);
        const ticks = [0, .2, .4, .6, .8, 1].map((r)=>{
            const ts = startMs + r * (endMs - startMs);
            return labelFor(ts, rangeKey);
        });
        return `
            <div class="ym-graph-stage" id="ym-graph-stage">
                <svg class="ym-graph" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-label="Market probability graph">
                    <line x1="${PAD_L}" y1="${toY(100)}" x2="${W-PAD_R}" y2="${toY(100)}" class="ym-grid"></line>
                    <line x1="${PAD_L}" y1="${toY(80)}" x2="${W-PAD_R}" y2="${toY(80)}" class="ym-grid"></line>
                    <line x1="${PAD_L}" y1="${toY(60)}" x2="${W-PAD_R}" y2="${toY(60)}" class="ym-grid"></line>
                    <line x1="${PAD_L}" y1="${toY(40)}" x2="${W-PAD_R}" y2="${toY(40)}" class="ym-grid"></line>
                    <line x1="${PAD_L}" y1="${toY(20)}" x2="${W-PAD_R}" y2="${toY(20)}" class="ym-grid"></line>
                    <line x1="${PAD_L}" y1="${toY(0)}" x2="${W-PAD_R}" y2="${toY(0)}" class="ym-grid"></line>
                    ${path ? `<path d="${path}" class="ym-line up"></path>` : ''}
                    <circle cx="${lastX}" cy="${lastY}" r="3.4" class="ym-point"></circle>
                    <text x="${W-PAD_R+8}" y="${toY(100)+4}" class="ym-ylabel">100%</text>
                    <text x="${W-PAD_R+8}" y="${toY(80)+4}" class="ym-ylabel">80%</text>
                    <text x="${W-PAD_R+8}" y="${toY(60)+4}" class="ym-ylabel">60%</text>
                    <text x="${W-PAD_R+8}" y="${toY(40)+4}" class="ym-ylabel">40%</text>
                    <text x="${W-PAD_R+8}" y="${toY(20)+4}" class="ym-ylabel">20%</text>
                    <text x="${W-PAD_R+8}" y="${toY(0)+4}" class="ym-ylabel">0%</text>
                </svg>
                <div class="ym-hover-line hidden" id="ym-hover-line"></div>
                <div class="ym-hover-dot hidden" id="ym-hover-dot"></div>
                <div class="ym-tooltip hidden" id="ym-tooltip"></div>
            </div>
            <div class="ym-xlabels">
                <span>${ticks[0]}</span>
                <span>${ticks[1]}</span>
                <span>${ticks[2]}</span>
                <span>${ticks[3]}</span>
                <span>${ticks[4]}</span>
                <span>${ticks[5]}</span>
            </div>
            <div class="ym-axis-caption">Market day starts ${ticks[0]}</div>
        `;
    }

    host.innerHTML = `
        <div class="yahoo-market-card yahoo-graph-card">
            <div class="ym-brand-row">
                <a class="ym-brand" href="${marketUrl}" target="_blank" rel="noopener" aria-label="Open Polymarket">
                    <span>Polymarket</span>
                </a>
                <a class="ym-market-link" href="${marketUrl}" target="_blank" rel="noopener">View Market <span aria-hidden="true">›</span></a>
            </div>
            <div class="ym-market-title-row">
                <span class="ym-bitcoin" aria-hidden="true">₿</span>
                <a class="ym-market-title" href="${marketUrl}" target="_blank" rel="noopener">${title}</a>
            </div>
            <div class="ym-topline">
                <div class="ym-legend">
                    <span class="ym-dot"></span>
                    <span class="ym-main" id="ym-main">${upPct.toFixed(0)}% chance</span>
                    <span class="ym-delta pos" id="ym-delta">+0.0%</span>
                </div>
                <div class="ym-ranges">
                    <button type="button" data-range="MKT">MKT</button>
                    <button type="button" data-range="3H">3H</button>
                    <button type="button" data-range="1H">1H</button>
                </div>
            </div>
            <div class="ym-mini-title">${title}</div>
            <div class="ym-graph-wrap" id="ym-graph-wrap"></div>
        </div>
    `;

    const graphWrap = host.querySelector('#ym-graph-wrap');
    const main = host.querySelector('#ym-main');
    const deltaEl = host.querySelector('#ym-delta');
    const buttons = Array.from(host.querySelectorAll('.ym-ranges button'));

    function bindHover(data, bounds){
        const stage = graphWrap?.querySelector('#ym-graph-stage');
        const line = graphWrap?.querySelector('#ym-hover-line');
        const dot = graphWrap?.querySelector('#ym-hover-dot');
        const tip = graphWrap?.querySelector('#ym-tooltip');
        if (!stage || !line || !dot || !tip || !data.length) return;

        const startMs = Number(bounds?.startMs || Date.now() - 3600000);
        const endMs = Math.max(startMs + 1000, Number(bounds?.endMs || Date.now()));
        const xForTs = (ts)=>{
            const t = Math.max(startMs, Math.min(endMs, Number(ts) || startMs));
            const ratio = (t - startMs) / Math.max(1, endMs - startMs);
            return Math.round(PAD_L + ratio * (W - PAD_L - PAD_R));
        };
        const hideHover = ()=>{
            line.classList.add('hidden');
            dot.classList.add('hidden');
            tip.classList.add('hidden');
        };
        const showHover = (evt)=>{
            const rect = stage.getBoundingClientRect();
            if (!rect.width || !rect.height) return;
            const plotLeft = (PAD_L / W) * rect.width;
            const plotRight = ((W - PAD_R) / W) * rect.width;
            const x = Math.max(plotLeft, Math.min(plotRight, evt.clientX - rect.left));
            let nearest = 0;
            let nearestDist = Infinity;
            for (let i = 0; i < data.length; i++){
                const px = (xForTs(data[i].t) / W) * rect.width;
                const d = Math.abs(px - x);
                if (d < nearestDist){
                    nearest = i;
                    nearestDist = d;
                }
            }
            const p = data[nearest];
            const px = (xForTs(p.t) / W) * rect.width;
            const py = (toY(p.up) / H) * rect.height;
            const upValue = pctValue(p.up);
            const stamp = hoverTime(p.t);

            line.style.left = `${px}px`;
            dot.style.left = `${px}px`;
            dot.style.top = `${py}px`;
            tip.textContent = `${stamp}  ${upValue.toFixed(2)}%`;
            const tipPad = 12;
            const tipWidth = Math.max(120, tip.offsetWidth || 140);
            const left = Math.max(tipPad, Math.min(rect.width - tipWidth - tipPad, px - (tipWidth / 2)));
            tip.style.left = `${left}px`;
            tip.style.top = `8px`;

            line.classList.remove('hidden');
            dot.classList.remove('hidden');
            tip.classList.remove('hidden');
        };
        stage.addEventListener('mousemove', showHover);
        stage.addEventListener('mouseenter', showHover);
        stage.addEventListener('mouseleave', hideHover);
        stage.addEventListener('touchstart', (evt)=> showHover(evt.touches[0]), { passive:true });
        stage.addEventListener('touchmove', (evt)=> showHover(evt.touches[0]), { passive:true });
        stage.addEventListener('touchend', hideHover);
    }

    const renderRange = (rangeKey)=>{
        const ranged = pointsForRange(rangeKey);
        const data = ranged.points;
        const first = data.length ? pctValue(data[0].up) : upPct;
        const last = data.length ? pctValue(data[data.length-1].up) : upPct;
        const dlt = last - first;
        if (main) main.textContent = `Up ${last.toFixed(0)}% chance`;
        if (deltaEl){
            deltaEl.textContent = `${dlt >= 0 ? '+' : ''}${dlt.toFixed(1)}%`;
            deltaEl.classList.toggle('pos', dlt >= 0);
            deltaEl.classList.toggle('neg', dlt < 0);
        }
        if (graphWrap) {
            graphWrap.innerHTML = chartMarkup(data, rangeKey, ranged);
            bindHover(data, ranged);
        }
        buttons.forEach((b)=> b.classList.toggle('active', b.dataset.range === rangeKey));
        marketRangePreference = rangeKey;
    };

    buttons.forEach((b)=> b.addEventListener('click', ()=> renderRange(b.dataset.range || 'MKT')));
    const allowed = new Set(['MKT', '3H', '1H']);
    renderRange(allowed.has(preferredRange) ? preferredRange : 'MKT');
}

function stopMarketLiveUpdates(){
    if (marketLiveTimer){
        clearInterval(marketLiveTimer);
        marketLiveTimer = null;
    }
}

async function fetchLocalMarketSeries(active, slug){
    const activeDate = String(active?.date || '');
    const [dayResp, curResp, gammaEvent] = await Promise.all([
        fetch(`/api/prices/day?date=${encodeURIComponent(activeDate)}`, { headers: authHeaders() }),
        fetch('/api/prices/current', { headers: authHeaders() }),
        fetchPolymarketEventBySlug(slug).catch(() => null),
    ]);
    if (!dayResp.ok || !curResp.ok) return null;
    const day = await dayResp.json().catch(()=>({}));
    const merged = {
        points: (Array.isArray(day?.points) ? day.points : []),
    };
    const cur = await curResp.json().catch(()=>({}));
    const gammaCur = currentFromGammaEvent(gammaEvent, slug);
    const bestCur = preferBestCurrent(cur, gammaCur);
    if (!currentLooksUsable(bestCur)) {
        throw new Error('Missing current market price from both local and Gamma sources');
    }
    const activeWithGamma = applyGammaEventToActive(active, gammaEvent, slug);
    return { merged, cur: bestCur, active: activeWithGamma };
}

async function renderPolymarketFallback(host, slug){
    const scriptSrc = 'https://embed.polymarket.com';
    let script = document.querySelector(`script[src="${scriptSrc}"]`);
    if (!script) {
        script = document.createElement('script');
        script.type = 'module';
        script.src = scriptSrc;
        document.head.appendChild(script);
    }
    if (!customElements.get('polymarket-market-embed')) {
        // Do not wait forever if the script/CORS/network blocks.
        await Promise.race([
            customElements.whenDefined('polymarket-market-embed'),
            new Promise((_, reject)=> setTimeout(()=> reject(new Error('Polymarket embed definition timeout')), 3000)),
        ]);
    }
    const embed = document.createElement('polymarket-market-embed');
    embed.setAttribute('market', slug);
    embed.setAttribute('theme', 'dark');
    embed.setAttribute('width', '392');
    embed.setAttribute('height', '180');
    embed.style.width = '100%';
    embed.style.height = '180px';
    embed.style.maxWidth = '392px';
    embed.style.minHeight = '180px';
    embed.style.display = 'block';
    embed.setAttribute('role', 'img');
    embed.setAttribute('aria-label', 'Bitcoin prediction market chart from Polymarket');
    host.innerHTML = '';
    host.appendChild(embed);
}

        // Custom prediction-market card first; plain Polymarket embed is last-resort fallback.
async function updateLegacyPolymarketEmbed(opts = {}) {
    const showLoading = opts.showLoading !== false;
    const setupLive = opts.setupLive !== false;
    const allowFastPath = opts.allowFastPath !== false;
    if (marketEmbedInFlight && !showLoading){
        return;
    }
    marketEmbedInFlight = true;
    try {
        const host = document.getElementById('polymarket-embed-host');
        if (!host) return;
        host.style.width = '100%';
        host.style.maxWidth = '100%';
        host.style.height = '400px';
        host.style.minHeight = '400px';
        host.style.display = 'flex';
        host.style.alignItems = 'center';
        host.style.justifyContent = 'center';
        host.style.position = 'relative';
        host.style.overflow = 'hidden';
        host.style.margin = '0 auto';
        host.style.backgroundColor = 'var(--panel)';
        host.style.borderRadius = '8px';

        if (showLoading){
            setMarketHostLoading(host);
        } else {
            clearMarketLoadingState(host);
        }

        const seriesStale = !marketSeriesLastRefreshMs || (Date.now() - marketSeriesLastRefreshMs >= MARKET_SERIES_REFRESH_MS);
        if (!showLoading && !setupLive && allowFastPath && !seriesStale && marketSeriesCache?.active && marketSeriesCache?.slug && marketSeriesCache?.merged){
            const curResp = await fetch('/api/prices/current', { headers: authHeaders() });
            if (curResp.ok){
                const cur = await curResp.json().catch(()=>({}));
                if (currentLooksUsable(cur)) {
                    const liveSeries = withLatestCurrentPoint(marketSeriesCache.merged, cur);
                    renderLocalGraphCard(host, marketSeriesCache.slug, marketSeriesCache.active, liveSeries, cur, marketRangePreference);
                    return;
                }
            }
        }

        const activeResp = await fetch('/api/active-market', { headers: authHeaders() });
        if (!activeResp.ok){
            stopMarketLiveUpdates();
            await ensureMarketLoaderVisible(host, showLoading);
            host.innerHTML = '<div class="market-error">Market unavailable</div>';
            clearMarketLoadingState(host);
            return;
        }
        const active = await activeResp.json();
        const slug = active?.market?.slug;
        if (!slug){
            stopMarketLiveUpdates();
            await ensureMarketLoaderVisible(host, showLoading);
            host.innerHTML = '<div class="market-error">No market data</div>';
            clearMarketLoadingState(host);
            return;
        }

        let renderedYahoo = false;
        let localRenderError = null;

        if (!renderedYahoo){
            try{
                const local = await fetchLocalMarketSeries(active, slug);
                if (local){
                    await ensureMarketLoaderVisible(host, showLoading);
                    marketSeriesCache = { active: local.active || active, slug, merged: local.merged };
                    marketSeriesLastRefreshMs = Date.now();
                    const liveSeries = withLatestCurrentPoint(local.merged, local.cur);
                    renderLocalGraphCard(host, slug, local.active || active, liveSeries, local.cur, marketRangePreference);
                    clearMarketLoadingState(host);
                    if (setupLive){
                        stopMarketLiveUpdates();
                        marketLiveTimer = setInterval(()=>{
                            updateLegacyPolymarketEmbed({ showLoading:false, setupLive:false }).catch(()=>{});
                        }, Math.max(1000, MARKET_LIVE_REFRESH_MS));
                    }
                    renderedYahoo = true;
                }
            } catch (err) {
                localRenderError = err;
            }
        }

        if (!renderedYahoo){
            stopMarketLiveUpdates();
            await ensureMarketLoaderVisible(host, showLoading);
            try {
                await renderPolymarketFallback(host, slug);
            } catch (fallbackErr) {
                const reason = localRenderError?.message || fallbackErr?.message || 'Unknown error';
                host.innerHTML = `<div class="market-error">Market load failed: ${reason}</div>`;
            } finally {
                clearMarketLoadingState(host);
            }
        }
    } catch (error) {
        console.error('Failed to load market card:', error);
        stopMarketLiveUpdates();
        const host = document.getElementById('polymarket-embed-host');
        if (host){
            await ensureMarketLoaderVisible(host, showLoading);
            host.innerHTML = '<div class="market-error">Failed to load market</div>';
            clearMarketLoadingState(host);
        }
    } finally {
        marketEmbedInFlight = false;
    }
}

function renderOfficialPolymarketEmbed(host, active){
    const slug = String(active?.market?.slug || '').trim();
    if (!slug) throw new Error('Active market slug unavailable');
    const title = String(
        active?.market?.question ||
        active?.market?.title ||
        'BTC Up or Down Daily'
    ).trim();
    const marketUrl = `https://polymarket.com/event/${encodeURIComponent(slug)}`;
    const embedUrl = new URL('https://embed.polymarket.com/market');
    embedUrl.searchParams.set('market', slug);
    embedUrl.searchParams.set('theme', 'dark');
    embedUrl.searchParams.set('volume', 'false');
    embedUrl.searchParams.set('buttons', 'false');
    embedUrl.searchParams.set('border', 'false');
    // The 2px dark mask on each side belongs to the outer figure, so the
    // iframe's requested viewport is four pixels narrower than its host.
    const embedWidth = Math.round(Math.min(1000, Math.max(320, (host.clientWidth || 904) - 4)));
    const embedHeight = embedWidth >= 760 ? 440 : (embedWidth >= 520 ? 420 : 400);
    embedUrl.searchParams.set('width', String(embedWidth));
    embedUrl.searchParams.set('height', String(embedHeight));

    const figure = document.createElement('figure');
    figure.className = 'polymarket-embed official-polymarket-embed';
    figure.id = `polymarket-${slug}`;
    figure.setAttribute('aria-label', `Polymarket prediction market: ${title}`);
    figure.setAttribute('itemscope', '');
    figure.setAttribute('itemtype', 'https://schema.org/WebPage');
    figure.style.setProperty('--embed-height', `${embedHeight}px`);

    const iframe = document.createElement('iframe');
    iframe.title = `${title} — Polymarket Prediction Market`;
    iframe.src = embedUrl.toString();
    iframe.width = String(embedWidth);
    iframe.height = String(embedHeight);
    iframe.setAttribute('frameborder', '0');
    iframe.setAttribute('allowtransparency', 'true');
    iframe.setAttribute('loading', 'eager');
    iframe.setAttribute('scrolling', 'no');

    const makeEmbedLink = (className, label) => {
        const anchor = document.createElement('a');
        anchor.href = marketUrl;
        anchor.target = '_blank';
        anchor.rel = 'noopener';
        anchor.className = `polymarket-embed-link ${className}`;
        anchor.setAttribute('aria-label', label);
        return anchor;
    };
    const brandLink = makeEmbedLink('polymarket-embed-brand-link', 'Open Polymarket');
    const titleLink = makeEmbedLink('polymarket-embed-title-link', `Open ${title} on Polymarket`);
    const link = makeEmbedLink('polymarket-embed-view-link', 'View market on Polymarket');

    const caption = document.createElement('figcaption');
    caption.className = 'sr-only';
    caption.textContent = `${title}. View full market and trade on Polymarket.`;

    figure.append(iframe, brandLink, titleLink, link, caption);
    host.replaceChildren(figure);

    let structured = document.getElementById('polymarket-market-structured-data');
    if (!structured){
        structured = document.createElement('script');
        structured.id = 'polymarket-market-structured-data';
        structured.type = 'application/ld+json';
        document.head.appendChild(structured);
    }
    structured.textContent = JSON.stringify({
        '@context': 'https://schema.org',
        '@type': 'WebPage',
        name: title,
        description: `Prediction market: ${title} on Polymarket.`,
        url: marketUrl,
        publisher: {
            '@type': 'Organization',
            name: 'Polymarket',
            url: 'https://polymarket.com',
        },
    });
}

async function updatePolymarketEmbed(opts = {}) {
    return updateLegacyPolymarketEmbed(opts);
}

// User page
async function loadUserData(opts = {}){
    const preserve = opts.preserve === true && userStatsHasData;
    // Set loading state to prevent layout shifts
    const userPage = document.getElementById('page-user');
    if (userPage && !preserve) {
        userPage.setAttribute('data-loading', 'true');
    }
    stopMarketLiveUpdates();

    try {
        // Start status and market embed in parallel so neither blocks the other
        const statsPromise = isAdmin ? Promise.resolve() : loadUserStats(null, {preserve});
        const marketHost = document.getElementById('polymarket-embed-host');
        const keepMarketVisible = !!(marketHost && marketHost.childElementCount);
        const embedPromise = updatePolymarketEmbed({showLoading: !keepMarketVisible, setupLive:true, allowFastPath:true});

        const toolbar = document.getElementById('account-toolbar');
        const sel = document.getElementById('user-account');

        if (!isAdmin){
            // Hide account selector for regular users
            if (toolbar) toolbar.classList.add('hidden');
            // Load this user's trades concurrently as well
            const tradesPromise = currentUsername ? loadTrades(currentUsername) : Promise.resolve();
            await Promise.allSettled([statsPromise, embedPromise, tradesPromise]);
            return;
        }

        // Admin view: allow selecting any account or All
        const r = await fetch('/api/accounts', {headers: authHeaders()});
        if (!r.ok) { await Promise.allSettled([statsPromise, embedPromise]); return; }
        const js = await r.json();
        if (toolbar) toolbar.classList.remove('hidden');
        if (sel){
            sel.innerHTML='';
            const optAll=document.createElement('option'); optAll.value='*'; optAll.textContent='All accounts'; sel.appendChild(optAll);
            // Get admin users - hard-coded based on users.json
            const adminUsers = new Set(['Jaxon', 'somerichfish']);

            const labelForAccount = (name)=>{
                if (!name) return name;
                const isFounder = /^founders?$/i.test(name);
                if (isFounder) {
                    return 'Founders';
                }
                if (adminUsers.has(name)) {
                    return `${name} - Admin`;
                }
                return name;
            };

            // Remove duplicates and filter out unwanted accounts
            const uniqueAccounts = [...new Set(js.accounts||[])]
                .filter(a => a && a.toLowerCase() !== 'chart_cache');

            // Keep all accounts including Founders
            const accounts = uniqueAccounts;

        // Sort accounts: Founders first (after All accounts), then current user, then other admins, then regular users
        const sortedAccounts = accounts.sort((a, b) => {
            const aIsFounders = /^founders?$/i.test(a);
            const bIsFounders = /^founders?$/i.test(b);
            const aIsCurrentUser = (a === currentUsername);
            const bIsCurrentUser = (b === currentUsername);
            const aIsAdmin = adminUsers.has(a);
            const bIsAdmin = adminUsers.has(b);

            // Founders always comes first (after All accounts)
            if (aIsFounders && !bIsFounders) return -1;
            if (!aIsFounders && bIsFounders) return 1;

            // Then current user (if not Founders)
            if (aIsCurrentUser && !bIsCurrentUser && !aIsFounders) return -1;
            if (!aIsCurrentUser && bIsCurrentUser && !bIsFounders) return 1;

            // Then other admins
            if (aIsAdmin && !bIsAdmin) return -1;
            if (!aIsAdmin && bIsAdmin) return 1;

            // Finally alphabetical
            return a.localeCompare(b);
        });

        sortedAccounts.forEach(a=>{
            const opt=document.createElement('option');
            opt.value=a;
            opt.textContent=labelForAccount(a);
            sel.appendChild(opt);
        });
        initUserAccountMenu();
        if (sel.options.length){
            const preferred = Array.from(sel.options).find((opt)=> opt.value === currentUsername)
                || sel.options[1]
                || sel.options[0];
            sel.value = preferred.value;
            syncUserAccountMenuFromSelect();
            await loadTrades(sel.value);
            if (sel.value !== '*') await loadUserStats(sel.value, {preserve:true, force:true});
        } else {
            syncUserAccountMenuFromSelect();
        }
        sel.onchange = ()=>{
            syncUserAccountMenuFromSelect();
            loadTrades(sel.value);
            const statsAccount = sel.value === '*' ? currentUsername : sel.value;
            loadUserStats(statsAccount, {preserve:false, force:true});
        };
    }
    // Ensure the parallel tasks finish before we complete load
    await Promise.allSettled([statsPromise, embedPromise]);


    } catch (error) {
        console.error('Error loading user data:', error);
    } finally {
        // Remove loading state
        if (userPage) {
            userPage.removeAttribute('data-loading');
        }
    }
}

async function loadUserStats(accountName = null, opts = {}) {
    const requestedAccount = String(accountName || currentUsername || '').trim();
    const preserve = opts.preserve === true && userStatsHasData;
    const force = opts.force === true;
    if (!force && userStatsHasData && userStatsAccountName === requestedAccount && (Date.now() - userStatsLastRefreshMs) < USER_STATS_CACHE_MS) return;
    const requestId = ++userStatsRequestId;
    const statusIndicator = document.getElementById('status-indicator');
    const statusText = document.getElementById('status-text');
    const totalProfitElement = document.getElementById('user-total-profit');
    const currentStakeElement = document.getElementById('user-current-stake');
    const safeBalanceElement = document.getElementById('user-safe-balance');

    console.log('[loadUserStats] called');
    if (statusIndicator && !preserve) {
        statusIndicator.textContent = '';
        statusIndicator.style.display = 'none';
    }
    const botStatus = document.getElementById('user-bot-status');
    if (botStatus && !preserve) {
        botStatus.classList.add('loading-text');
    }
    if (statusText && !preserve) {
        statusText.textContent = '';
        statusText.setAttribute('aria-label', 'Loading');
        statusText.classList.remove('loading-inline');
    }
    if (!preserve) [totalProfitElement, currentStakeElement, safeBalanceElement]
        .forEach((el)=>{
            if (!el) return;
            el.classList.add('loading-text');
            el.textContent = '';
            el.setAttribute('aria-label', 'Loading');
        });

    try {
        console.log('[loadUserStats] Fetching account summary...');
        const summaryUrl = accountName ? `/api/user/summary?account=${encodeURIComponent(accountName)}` : '/api/user/summary';
        const summaryPromise = fetch(summaryUrl, { headers: authHeaders() });
        let summary = {};

        // The summary is account-scoped for admins and includes the selected
        // account's pause state. Ignore responses superseded by a newer choice.
        try {
            const summaryResponse = await summaryPromise;
            if (!summaryResponse.ok) throw new Error(`Summary API failed: ${summaryResponse.status}`);
            summary = await summaryResponse.json();
            if (requestId !== userStatsRequestId) return;
            console.log('[loadUserStats] Summary loaded:', summary);
            // Update total profit
            if (totalProfitElement && summary.total_profit !== undefined) {
                const profit = parseFloat(summary.total_profit) || 0;
                totalProfitElement.textContent = `$${profit.toFixed(2)}`;
                totalProfitElement.removeAttribute('aria-label');
                totalProfitElement.classList.remove('positive', 'negative');
                totalProfitElement.classList.add(profit >= 0 ? 'positive' : 'negative');
                console.log('[loadUserStats] Profit updated:', profit);
            }
            // Display the persisted noon-to-noon budget, not position market value.
            if (currentStakeElement) {
                const rawStake = Number(summary.current_stake ?? 0);
                const currentStake = Number.isFinite(rawStake) ? rawStake : 0;
                currentStakeElement.textContent = `$${currentStake.toFixed(2)}`;
                currentStakeElement.removeAttribute('aria-label');
                console.log('[loadUserStats] Stake updated:', currentStake);
            }
            if (safeBalanceElement) {
                const raw = Number(summary.total_balance ?? summary.safe_balance ?? summary.holdings_value ?? 0);
                const safeBal = Number.isFinite(raw) ? raw : 0;
                safeBalanceElement.textContent = `$${safeBal.toFixed(2)}`;
                safeBalanceElement.removeAttribute('aria-label');
            }
            [totalProfitElement, currentStakeElement, safeBalanceElement]
                .forEach((el)=> el && el.classList.remove('loading-text'));
        } catch (e) {
            console.warn('Summary load failed or incomplete:', e);
            throw e;
        }

        if (requestId !== userStatsRequestId) return;
        // Update trading status based on the selected account's pause setting.
        if (statusIndicator && statusText) {
            const isPaused = summary.paused === true;
            console.log('[loadUserStats] Account paused status:', isPaused);
            statusIndicator.style.display = '';
            statusIndicator.textContent = '●';

            if (isPaused) {
                statusIndicator.className = 'status-indicator paused';
                statusText.textContent = 'Paused';
                statusText.removeAttribute('aria-label');
                statusText.classList.remove('loading-inline');
                if (botStatus) {
                    botStatus.classList.remove('loading-text');
                }
                console.log('[loadUserStats] Status set to Paused');
            } else {
                statusIndicator.className = 'status-indicator running';
                statusText.textContent = 'Active';
                statusText.removeAttribute('aria-label');
                statusText.classList.remove('loading-inline');
                if (botStatus) {
                    botStatus.classList.remove('loading-text');
                }
                console.log('[loadUserStats] Status set to Active');
            }
        } else {
            console.error('[loadUserStats] Status elements not found');
        }

        userStatsHasData = true;
        userStatsLastRefreshMs = Date.now();
        userStatsAccountName = requestedAccount;

    } catch (error) {
        console.error('[loadUserStats] Error loading user stats:', error);

        // Set fallback values with proper error handling
        if (!preserve && totalProfitElement) totalProfitElement.textContent = '$0.00';
        if (!preserve && currentStakeElement) currentStakeElement.textContent = '$0.00';
        if (!preserve && safeBalanceElement) safeBalanceElement.textContent = '$0.00';
        if (totalProfitElement) totalProfitElement.removeAttribute('aria-label');
        if (currentStakeElement) currentStakeElement.removeAttribute('aria-label');
        if (safeBalanceElement) safeBalanceElement.removeAttribute('aria-label');
        if (statusText) {
            statusText.removeAttribute('aria-label');
            statusText.classList.remove('loading-inline');
        }
        if (statusIndicator) statusIndicator.style.display = '';
        if (botStatus) {
            botStatus.classList.remove('loading-text');
        }
        [totalProfitElement, currentStakeElement, safeBalanceElement]
            .forEach((el)=> el && el.classList.remove('loading-text'));

        // Keep default "Active" status on error
        console.log('[loadUserStats] Keeping default Active status due to error');
    }
}

function mapEvent(ev){
    const e = String(ev||'').toUpperCase();
    if (e.includes('BUY') && e.includes('UP')) return 'buy up';
    if (e.includes('BUY') && e.includes('DOWN')) return 'buy down';
    if (e.includes('SELL') && e.includes('UP')) return 'sell up';
    if (e.includes('SELL') && e.includes('DOWN')) return 'sell down';
    if (e.includes('WIN')) return 'win';
    if (e.includes('LOSE') || e.includes('LOSS')) return 'lose';
    return e.replaceAll('_',' ').toLowerCase();
}
function fmtShortTime(ts){
    try{
        const d = new Date(ts);
        if (isNaN(d.getTime())) return String(ts||'');
        const now = new Date();
        const sameDay = d.getFullYear()===now.getFullYear() && d.getMonth()===now.getMonth() && d.getDate()===now.getDate();
        const opts = sameDay? {hour:'numeric', minute:'2-digit'} : {month:'short', day:'numeric', hour:'numeric', minute:'2-digit'};
        return new Intl.DateTimeFormat(undefined, opts).format(d);
    }catch{ return String(ts||''); }
}
async function loadTrades(name){
    let js;
    if (name === '*' && isAdmin){
        const rAll = await fetch('/api/trades/all', {headers: authHeaders()});
        js = await rAll.json();
    } else {
        const r = await fetch(`/api/account/${encodeURIComponent(name)}/trades`, {headers: authHeaders()});
        js = await r.json();
    }
    const body = $('#trades-body');
    body.innerHTML = '';
    const wrap = document.getElementById('trades-wrap');
    const empty = document.getElementById('user-empty');
    const arr = Array.isArray(js.trades) ? js.trades : [];
    arr.sort((a,b)=>{
        const ta = new Date(a.ts||0).getTime();
        const tb = new Date(b.ts||0).getTime();
        return tb - ta;
    });
    if (!arr.length){
        if (wrap) wrap.classList.add('hidden');
        if (empty) empty.classList.remove('hidden');
        return;
    }
    if (empty) empty.classList.add('hidden');
    if (wrap) wrap.classList.remove('hidden');
    for (const r of arr){
        const tr = document.createElement('tr');
        const tdTime = document.createElement('td'); tdTime.textContent = fmtShortTime(r.ts);
        const eventLabel = mapEvent(r.event);
        const tdEv = document.createElement('td');
        const eventPill = document.createElement('span');
        eventPill.className = `trade-event trade-event-${eventLabel.replace(/\s+/g, '-')}`;
        eventPill.textContent = eventLabel;
        tdEv.appendChild(eventPill);
        const tdSz = document.createElement('td'); tdSz.textContent = (Number(r.size)||0).toFixed(4);
        const tdPx = document.createElement('td'); tdPx.textContent = (Number(r.price)||0).toFixed(2);
        const tdUsd = document.createElement('td'); tdUsd.textContent = (Number(r.size||0)*Number(r.price||0)).toFixed(2);
        tr.append(tdTime, tdEv, tdSz, tdPx, tdUsd);
        body.appendChild(tr);
    }
}
function ensureUserChart(){
    if (userChart) return userChart;
    const ctx = $('#userChart');
    if (!ctx){ return null; }
    // x-axis starts at 9:00 AM in selected timezone
    const start9 = nineAmUtcToday(userTimezone);
    userChart = new Chart(ctx, {
        type:'line',
        data:{ datasets:[
            {
                label:'UP %',
                data:[],
                borderColor:'#ffa726',
                backgroundColor:'transparent',
                tension:0,
                stepped:true,
                fill:false,
                spanGaps:true,
                pointRadius: function(context) {
                    // Show points only at hour marks (minute 0)
                    if (!context.parsed || !context.parsed.x) return 0;
                    const date = new Date(context.parsed.x);
                    return date.getMinutes() === 0 ? 4 : 0;
                },
                pointHoverRadius:8,
                pointHitRadius:15,
                pointStyle:'circle'
            }
        ]},
        options:{
            plugins:{legend:{display:true}},
            parsing:false,
            scales:{
                x:{
                    type:'time',
                    time:{ unit:'hour', displayFormats:{ minute:'h:mm a', hour:'h a' } },
                    min: start9,
                    max: start9 + 24*60*60*1000,
                    ticks:{ maxRotation:0, autoSkip:true, source:'auto' }
                },
                y:{ beginAtZero:true, min:0, max:100, ticks:{ callback:(v)=> v+"%" } }
            }
        }
    });
    try{ userChart.update(); }catch(_){ }
    return userChart;
}
    async function startUserLive(){
        const c = ensureUserChart(); if (!c) return;
        if (userLiveTimer) clearInterval(userLiveTimer);
        const tick = async ()=>{
            try{
                const pr = await fetch('/api/prices/current', {headers: authHeaders()});
                const su = await fetch('/api/user/summary', {headers: authHeaders()});
                if (pr.ok){ const p = await pr.json();
                    $('#today-date').textContent = 'date: ' + p.date;
                    const t = Date.now();
                    const up = +(p.up.mid||p.up.ask||p.up.bid||0);
                    const dn = +(p.down.mid||p.down.ask||p.down.bid||0);
                    const y = computeUpPct(up, dn);
                    if (y!=null){
                        const ds = userChart.data.datasets[0];
                        ds.data.push({x:t, y});
                        if (ds.data.length>120) ds.data.splice(0, ds.data.length-120);
                        userChart.update('none');
                    }
                }
                if (su.ok){ const s = await su.json();
                    const up_sz = +(s.positions.UP.size||0);
                    const up_val = +(s.mark.UP||0);
                    $('#today-up').textContent = `UP: ${up_sz.toFixed(4)} ($${up_val.toFixed(2)})`;
                }
            }catch{}
        };
        tick();
        userLiveTimer = setInterval(tick, 58000);
    }

// Removed day-picker and live button code (no longer part of the UI)

// Logs stream
async function streamLogs(){
    const res = await fetch('/api/logs/stream', {headers: authHeaders()});
    if (!res.ok) return;
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    async function pump(){
        const {value, done} = await reader.read();
        if (done) return;
        $('#logs').textContent += dec.decode(value);
        $('#logs').scrollTop = $('#logs').scrollHeight;
        pump();
    }
    pump();
}

// Initial state: avoid flash by routing synchronously before any async fetch
(async function initRoute(){
const authed = !!getToken();
if (authed) {
    try { await postLogin(); } catch(e) {}
}
// Correctly route based on the initial path, which might have been updated by postLogin
if (!routeFromHash()) {
    routeFromLocation();
}
document.body.setAttribute('data-js-ready','1');
})();

// Profile: load/save
// 3-letter DST-aware abbreviations (US) mapped to IANA for storage
const US_TZ_ABBR_TO_IANA = {
    // Base labels only; UI shows PST/MST/CST/EST, DST auto-applies based on IANA
    'PST': 'America/Los_Angeles',
    'MST': 'America/Denver',
    'CST': 'America/Chicago',
    'EST': 'America/New_York'
};
function isDstNow(tz){
    const now = new Date();
    const offNow = tzOffsetMs(tz, now);
    const offJan = tzOffsetMs(tz, new Date(Date.UTC(now.getUTCFullYear(),0,1)));
    const offJul = tzOffsetMs(tz, new Date(Date.UTC(now.getUTCFullYear(),6,1)));
    const dstOff = Math.min(offJan, offJul); // more negative offset is DST in US
    return offNow === dstOff;
}
function currentAbbrForIana(tz){
    if (!tz) return 'PST';
    if (tz.includes('Los_Angeles')) return isDstNow(tz)? 'PDT':'PST';
    if (tz.includes('Denver'))      return isDstNow(tz)? 'MDT':'MST';
    if (tz.includes('Chicago'))     return isDstNow(tz)? 'CDT':'CST';
    if (tz.includes('New_York'))    return isDstNow(tz)? 'EDT':'EST';
    return 'PST';
}
function populateTimezones(){
    const sel = $('#pf-timezone'); if (!sel) return;
    const menu = $('#pf-timezone-menu');
    sel.innerHTML = '';
    if (menu) menu.innerHTML = '';
    ['PST','MST','CST','EST'].forEach(ab=>{
        const opt=document.createElement('option'); opt.value=ab; opt.textContent=ab; sel.appendChild(opt);
        if (menu){
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'tz-option';
            btn.dataset.value = ab;
            btn.setAttribute('role', 'option');
            btn.textContent = ab;
            btn.addEventListener('click', ()=>{
                setTimezoneUIValue(ab);
                closeTimezoneMenu();
            });
            menu.appendChild(btn);
        }
    });
}

function setTimezoneUIValue(value){
    const val = String(value || 'PST');
    const sel = $('#pf-timezone');
    if (sel) sel.value = val;
    const label = $('#pf-timezone-btn-label');
    if (label) label.textContent = val;
    $$('.tz-option').forEach((el)=>{
        el.classList.toggle('active', el.dataset.value === val);
        el.setAttribute('aria-selected', el.dataset.value === val ? 'true' : 'false');
    });
}

function closeTimezoneMenu(){
    const menu = $('#pf-timezone-menu');
    const btn = $('#pf-timezone-btn');
    if (menu) menu.classList.add('hidden');
    if (btn){
        btn.classList.remove('open');
        btn.setAttribute('aria-expanded', 'false');
    }
}

function initTimezoneMenu(){
    const btn = $('#pf-timezone-btn');
    const menu = $('#pf-timezone-menu');
    if (!btn || !menu || btn.dataset.bound === '1') return;
    btn.dataset.bound = '1';
    btn.addEventListener('click', (e)=>{
        e.preventDefault();
        const open = menu.classList.contains('hidden');
        if (open){
            menu.classList.remove('hidden');
            btn.classList.add('open');
            btn.setAttribute('aria-expanded', 'true');
        } else {
            closeTimezoneMenu();
        }
    });
    document.addEventListener('click', (e)=>{
        if (menu.classList.contains('hidden')) return;
        if (!menu.contains(e.target) && !btn.contains(e.target)){
            closeTimezoneMenu();
        }
    });
}
populateTimezones();
initTimezoneMenu();
setTimezoneUIValue('PST');

async function loadProfile(){
    const r = await fetch('/api/user/profile', {headers: authHeaders()});
    if (!r.ok) return;
    const js = await r.json();
    const uname = js.username || '';
    $('#pf-username-view').textContent = uname;
    const abbr = currentAbbrForIana(js.timezone || userTimezone);
    const base = (abbr||'').replace('DT','ST');
    const sel = document.getElementById('pf-timezone');
    if (sel){
        const nextTz = ['PST','MST','CST','EST'].includes(base) ? base : 'PST';
        setTimezoneUIValue(nextTz);
    }
    await loadMyTradingAccount();
    await loadProfileDeleteButton();
}

let tradingAccounts = [];
let myTradingAccount = null;
let adminDraggingAccountEl = null;
let adminLastDragClientY = null;
let adminDeleteTargetName = '';

function setUserAccountUIValue(value){
    const sel = document.getElementById('user-account');
    const label = document.getElementById('user-account-btn-label');
    const menu = document.getElementById('user-account-menu');
    const v = String(value ?? '');
    if (sel) sel.value = v;
    if (label && sel){
        const opt = Array.from(sel.options).find(o => o.value === v);
        label.textContent = opt ? opt.textContent : 'All accounts';
    }
    if (menu){
        Array.from(menu.querySelectorAll('.tz-option')).forEach((el)=>{
            const active = el.dataset.value === v;
            el.classList.toggle('active', active);
            el.setAttribute('aria-selected', active ? 'true' : 'false');
        });
    }
}

function closeUserAccountMenu(){
    const menu = document.getElementById('user-account-menu');
    const btn = document.getElementById('user-account-btn');
    if (menu) menu.classList.add('hidden');
    if (btn){
        btn.classList.remove('open');
        btn.setAttribute('aria-expanded', 'false');
    }
}

function syncUserAccountMenuFromSelect(){
    const sel = document.getElementById('user-account');
    const menu = document.getElementById('user-account-menu');
    if (!sel || !menu) return;
    menu.innerHTML = '';
    Array.from(sel.options).forEach((opt)=>{
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'tz-option';
        btn.dataset.value = opt.value;
        btn.setAttribute('role', 'option');
        btn.textContent = opt.textContent || opt.value;
        btn.addEventListener('click', ()=>{
            setUserAccountUIValue(opt.value);
            closeUserAccountMenu();
            sel.dispatchEvent(new Event('change', {bubbles:true}));
        });
        menu.appendChild(btn);
    });
    setUserAccountUIValue(sel.value || '*');
}

function initUserAccountMenu(){
    const btn = document.getElementById('user-account-btn');
    const menu = document.getElementById('user-account-menu');
    if (!btn || !menu || btn.dataset.bound === '1') return;
    btn.dataset.bound = '1';
    btn.addEventListener('click', (e)=>{
        e.preventDefault();
        const open = menu.classList.contains('hidden');
        if (open){
            menu.classList.remove('hidden');
            menu.classList.remove('open-up');
            const rect = btn.getBoundingClientRect();
            const menuHeight = Math.min(menu.scrollHeight, 320);
            const below = window.innerHeight - rect.bottom - 12;
            if (below < menuHeight && rect.top > below) menu.classList.add('open-up');
            btn.classList.add('open');
            btn.setAttribute('aria-expanded', 'true');
        } else {
            closeUserAccountMenu();
        }
    });
    document.addEventListener('click', (e)=>{
        if (menu.classList.contains('hidden')) return;
        if (!menu.contains(e.target) && !btn.contains(e.target)){
            closeUserAccountMenu();
        }
    });
}

function fmtMetaDate(value){
    if (!value) return 'n/a';
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return String(value);
    return d.toLocaleString();
}

function safeTxServiceHostsForChain(chainIdDec){
    const map = {
        1: ['https://safe-transaction-mainnet.safe.global'],
        10: ['https://safe-transaction-optimism.safe.global'],
        56: ['https://safe-transaction-bsc.safe.global'],
        100: ['https://safe-transaction-gnosis-chain.safe.global'],
        137: ['https://safe-transaction-polygon.safe.global'],
        42161: ['https://safe-transaction-arbitrum.safe.global'],
        43114: ['https://safe-transaction-avalanche.safe.global'],
        8453: ['https://safe-transaction-base.safe.global'],
    };
    const preferred = map[chainIdDec] || [];
    const all = Array.from(new Set(Object.values(map).flat()));
    return [...preferred, ...all.filter(h => !preferred.includes(h))];
}

async function detectSafesForOwner(owner, chainIdHex){
    const ownerAddr = String(owner || '').trim();
    if (!ownerAddr) return [];
    const chainIdDec = Number.parseInt(String(chainIdHex || '0x89'), 16) || 137;
    const r = await fetch(`/api/wallet/safes?owner=${encodeURIComponent(ownerAddr)}&chain_id=${encodeURIComponent(String(chainIdDec))}`, {
        headers: authHeaders(),
    });
    const js = await r.json().catch(()=>({}));
    if (!r.ok){
        const detail = js?.detail || `HTTP ${r.status}`;
        throw new Error(`Safe lookup failed: ${detail}`);
    }
    const raw = Array.isArray(js?.safes) ? js.safes : [];
    const safes = raw
        .map((s)=> typeof s === 'string' ? s : (s && typeof s === 'object' ? (s.address || s.value || '') : ''))
        .map((s)=> String(s || '').trim())
        .filter(Boolean);
    if (!safes.length && Array.isArray(js?.checked_chains)){
        throw new Error(`No Safe detected (checked chains: ${js.checked_chains.join(', ')})`);
    }
    return safes;
}

async function requestWalletAccount({ forcePrompt = false } = {}){
    // Best-effort: force a fresh MetaMask permission prompt for eth_accounts.
    if (forcePrompt){
        try {
            await window.ethereum.request({
                method: 'wallet_revokePermissions',
                params: [{ eth_accounts: {} }],
            });
        } catch (_) {}
    }
    try {
        await window.ethereum.request({
            method: 'wallet_requestPermissions',
            params: [{ eth_accounts: {} }],
        });
    } catch (_) {}
    const accounts = await window.ethereum.request({ method: 'eth_requestAccounts' });
    if (!Array.isArray(accounts) || !accounts.length || !accounts[0]){
        throw new Error('No wallet account returned.');
    }
    return String(accounts[0]);
}

async function connectWalletAddress({ requireSafe = true, forcePrompt = false, allowSavedSafeFallback = true } = {}){
    if (!window.ethereum || !window.ethereum.request){
        throw new Error('No wallet found. Install MetaMask or a compatible wallet.');
    }
    const eoa = await requestWalletAccount({ forcePrompt });
    const chainId = await window.ethereum.request({ method: 'eth_chainId' }).catch(()=>'0x89');
    let safes = [];
    try{
        safes = await detectSafesForOwner(eoa, chainId);
    } catch (err) {
        if (requireSafe) throw err;
        safes = [];
    }
    let safe = safes.length ? String(safes[0]) : null;
    if (!safe && allowSavedSafeFallback){
        // Fallback: use already-saved safe for this user if present.
        try{
            const existing = await fetch('/api/user/trading-account', { headers: authHeaders() });
            if (existing.ok){
                const js = await existing.json().catch(()=>({}));
                const acct = js?.account || {};
                const savedSafe = String(acct.safe_address || '').trim();
                const savedOwner = String(acct.wallet_owner || '').trim();
                if (savedSafe && (!savedOwner || savedOwner.toLowerCase() === eoa.toLowerCase())){
                    safe = savedSafe;
                }
            }
        } catch (_) {}
    }
    if (requireSafe && !safe){
        throw new Error('No Safe detected for this wallet on supported chains. Select a wallet that owns a Safe.');
    }
    return { eoa, funder: safe || eoa, safe, safes, chainId };
}

async function updateMyTradingAccountRequest(body){
    let last = null;
    for (const method of ['POST', 'PATCH', 'PUT']){
        const r = await fetch('/api/user/trading-account', {
            method,
            headers: {'Content-Type':'application/json', ...authHeaders()},
            body: JSON.stringify(body),
        });
        if (r.status === 405){
            last = r;
            continue;
        }
        return r;
    }
    return last;
}

function profileFunderValue(){
    // Backend funder should remain Safe/proxy address.
    const el = $('#pf-tr-safe');
    return (el?.dataset?.value || '').trim();
}

function profileWalletOwnerValue(){
    const el = $('#pf-tr-funder');
    return (el?.dataset?.value || '').trim();
}

function profileSafeValue(){
    const el = $('#pf-tr-safe');
    return (el?.dataset?.value || '').trim();
}

function enhanceSelect(select){
    if (!select || select.dataset.enhancedSelect === '1') return;
    select.dataset.enhancedSelect = '1';
    select.classList.add('sr-only-select');

    const shell = document.createElement('div');
    shell.className = 'app-select';
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'app-select-btn';
    button.setAttribute('aria-haspopup', 'listbox');
    button.setAttribute('aria-expanded', 'false');
    const valueLabel = document.createElement('span');
    valueLabel.className = 'app-select-value';
    const caret = document.createElement('span');
    caret.className = 'tz-caret';
    caret.setAttribute('aria-hidden', 'true');
    button.append(valueLabel, caret);
    const menu = document.createElement('div');
    menu.className = 'app-select-menu hidden';
    menu.setAttribute('role', 'listbox');
    shell.append(button, menu);
    select.parentNode.insertBefore(shell, select);
    shell.appendChild(select);

    const close = ()=>{
        menu.classList.add('hidden');
        button.classList.remove('open');
        button.setAttribute('aria-expanded', 'false');
    };
    const sync = ()=>{
        const selected = select.options[select.selectedIndex] || select.options[0];
        valueLabel.textContent = selected?.textContent || '';
        Array.from(menu.children).forEach((item)=>{
            const active = item.dataset.value === select.value;
            item.classList.toggle('active', active);
            item.setAttribute('aria-selected', active ? 'true' : 'false');
        });
        button.disabled = select.disabled;
    };
    const rebuild = ()=>{
        menu.innerHTML = '';
        Array.from(select.options).forEach((option)=>{
            const item = document.createElement('button');
            item.type = 'button';
            item.className = 'app-select-option';
            item.dataset.value = option.value;
            item.setAttribute('role', 'option');
            item.textContent = option.textContent;
            item.disabled = option.disabled;
            item.addEventListener('click', ()=>{
                select.value = option.value;
                select.dispatchEvent(new Event('change', {bubbles:true}));
                sync();
                close();
            });
            menu.appendChild(item);
        });
        sync();
    };
    button.addEventListener('click', (event)=>{
        event.preventDefault();
        event.stopPropagation();
        const opening = menu.classList.contains('hidden');
        document.querySelectorAll('.app-select-menu:not(.hidden)').forEach((other)=>other.classList.add('hidden'));
        document.querySelectorAll('.app-select-btn.open').forEach((other)=>{
            other.classList.remove('open');
            other.setAttribute('aria-expanded', 'false');
        });
        if (opening){
            menu.classList.remove('hidden');
            button.classList.add('open');
            button.setAttribute('aria-expanded', 'true');
        }
    });
    document.addEventListener('click', (event)=>{
        if (!shell.contains(event.target)) close();
    });
    select.addEventListener('change', sync);
    select._syncEnhancedSelect = sync;
    rebuild();
}

enhanceSelect(document.getElementById('pf-tr-auth-mode'));

function profileAuthModeValue(){
    const mode = String($('#pf-tr-auth-mode')?.value || 'session_key').trim();
    return mode === 'private_key' ? 'private_key' : 'session_key';
}

function updateProfileAuthModeUi(acct = myTradingAccount){
    const privateMode = profileAuthModeValue() === 'private_key';
    const pkWrap = $('#pf-tr-private-key-wrap');
    const pkActions = $('.profile-private-key-actions');
    const trBtn = $('#pf-tr-generate-trading');
    const feeBtn = $('#pf-tr-generate-fee');
    const hasKey = !!acct?.has_private_key;
    if (pkWrap) pkWrap.classList.toggle('hidden', !privateMode || hasKey);
    if (pkActions) pkActions.classList.toggle('hidden', !privateMode);
    if (trBtn) trBtn.disabled = privateMode;
    if (feeBtn) feeBtn.disabled = privateMode;
    const ind = $('#pf-tr-private-key-indicator');
    if (ind){
        ind.textContent = hasKey ? 'Private key configured' : 'No private key configured';
        ind.className = `key-indicator${hasKey ? ' ok' : ''}`;
    }
    const clearBtn = $('#pf-tr-clear-private-key');
    if (clearBtn) clearBtn.disabled = !hasKey;
}

function updateProfileTradingToggleUi(){
    const input = $('#pf-tr-enabled');
    const label = $('#pf-tr-enabled-label');
    if (!input || !label) return;
    label.textContent = input.checked ? 'Enabled' : 'Paused';
    label.classList.toggle('is-paused', !input.checked);
}

function setReadonlyAddressField(id, value){
    const el = $(id);
    if (!el) return;
    const v = String(value || '').trim();
    el.dataset.value = v;
    el.textContent = v || 'Not set';
    el.title = v || '';
}

async function persistProfileFunderIfPresent(extraBody = {}){
    const funder = profileFunderValue();
    const walletOwner = profileWalletOwnerValue();
    const safeAddress = profileSafeValue();
    if (!funder && !walletOwner && !safeAddress && !extraBody.wallet_owner && !extraBody.safe_address) return { ok: true };
    const body = { ...extraBody };
    if (funder) body.funder = funder;
    if (walletOwner && body.wallet_owner == null) body.wallet_owner = walletOwner;
    if (safeAddress && body.safe_address == null) body.safe_address = safeAddress;
    const r = await updateMyTradingAccountRequest(body);
    const js = await r.json().catch(()=>({}));
    if (!r.ok) return { ok: false, detail: js.detail || 'Failed to save funder' };
    return { ok: true };
}

function renderProfileTradingMeta(acct){
    const meta = $('#pf-tr-meta');
    const trInd = $('#pf-tr-trading-indicator');
    const feeInd = $('#pf-tr-fee-indicator');
    if (!meta) return;
    if (!acct){
        if (trInd) { trInd.textContent = 'Trading key not set'; trInd.className = 'key-indicator'; }
        if (feeInd) { feeInd.textContent = 'Fee key not set'; feeInd.className = 'key-indicator'; }
        meta.innerHTML = '<span>No trading account found yet. Save settings to create one.</span>';
        return;
    }
    if (trInd) {
        if (acct.auth_mode === 'private_key'){
            trInd.textContent = acct.has_private_key ? 'Private key active' : 'Private key not set';
            trInd.className = `key-indicator${acct.has_private_key ? ' ok' : ''}`;
        } else {
            trInd.textContent = !acct.has_trading_session_key ? 'Trading key not set' : acct.trading_key_expired ? 'Trading key expired' : 'Trading key set';
            trInd.className = `key-indicator${!acct.has_trading_session_key ? '' : acct.trading_key_expired ? ' expired' : ' ok'}`;
        }
    }
    if (feeInd) {
        if (acct.auth_mode === 'private_key'){
            feeInd.textContent = 'Fee uses private key';
            feeInd.className = `key-indicator${acct.has_private_key ? ' ok' : ''}`;
        } else {
            feeInd.textContent = !acct.has_fee_session_key ? 'Fee key not set' : acct.fee_key_expired ? 'Fee key expired' : 'Fee key set';
            feeInd.className = `key-indicator${!acct.has_fee_session_key ? '' : acct.fee_key_expired ? ' expired' : ' ok'}`;
        }
    }
    meta.innerHTML = `
        <span>Auth Mode: ${acct.auth_mode === 'private_key' ? 'Wallet Private Key' : 'Session Keys'}</span>
        <span>Trading Expires: ${acct.auth_mode === 'private_key' ? 'n/a' : fmtMetaDate(acct.trading_session_expires_at)}</span>
        <span>Fee Expires: ${acct.auth_mode === 'private_key' ? 'n/a' : fmtMetaDate(acct.fee_session_expires_at)}</span>
    `;
}

async function loadMyTradingAccount(){
    const r = await fetch('/api/user/trading-account', {headers: authHeaders()});
    if (!r.ok){
        renderProfileTradingMeta(null);
        return;
    }
    const js = await r.json();
    const acct = js.account || null;
    myTradingAccount = acct;

    // Profile toggle is user-level enable; admin-level enable is separate.
    const userEnabled = !!(acct?.user_enabled ?? false);
    const adminEnabled = !!(acct?.admin_enabled ?? false);
    const requiresFeeKey = !!(acct?.requires_fee_key ?? true);
    const pfEnabled = $('#pf-tr-enabled');
    if (pfEnabled){
        pfEnabled.checked = userEnabled;
        pfEnabled.disabled = false;
        pfEnabled.title = '';
        updateProfileTradingToggleUi();
    }
    // Keep local copy so save validation can use server-side privilege rules.
    if (myTradingAccount) myTradingAccount.requires_fee_key = requiresFeeKey;
    if (myTradingAccount) myTradingAccount.admin_enabled = adminEnabled;
    const authMode = $('#pf-tr-auth-mode');
    if (authMode){
        authMode.value = acct?.auth_mode === 'private_key' ? 'private_key' : 'session_key';
        authMode._syncEnhancedSelect?.();
    }
    const pkInput = $('#pf-tr-private-key');
    if (pkInput){
        pkInput.value = '';
        pkInput.placeholder = 'Enter private key';
    }
    setReadonlyAddressField('#pf-tr-funder', acct?.wallet_owner || '');
    setReadonlyAddressField('#pf-tr-safe', acct?.safe_address || '');
    renderProfileTradingMeta(acct);
    updateProfileAuthModeUi(acct);
}

async function loadAdminTradingAccounts(){
    const card = $('#trading-accounts-card');
    if (!card) return;
    if (!isAdmin){
        card.classList.add('hidden');
        return;
    }

    const r = await fetch('/api/trading/accounts', {headers: authHeaders()});
    if (!r.ok){
        card.classList.remove('hidden');
        $('#trading-accounts-list').innerHTML = '<p class="inline-hint">Failed to load trading accounts.</p>';
        return;
    }

    card.classList.remove('hidden');
    const js = await r.json();
    tradingAccounts = Array.isArray(js.accounts) ? js.accounts.slice() : [];
    tradingAccounts.sort((a,b)=> (Number(b.priority)||0) - (Number(a.priority)||0));
    renderTradingAccounts();
}

function renderTradingAccounts(){
    const root = $('#trading-accounts-list');
    if (!root) return;
    root.innerHTML = '';
    if (!tradingAccounts.length){
        root.innerHTML = '<p class="inline-hint">No trading accounts configured.</p>';
        return;
    }

    tradingAccounts.forEach((acct)=>{
        const privileged = !!(acct.is_admin_or_founders || String(acct.name || '').toLowerCase() === 'founders');
        const keyControlsHtml = privileged
            ? `<div class="trading-key-grid trading-key-grid-single">
                    <button class="btn outline ta-clear-trading" type="button">Revoke Trading Key</button>
               </div>`
            : `<div class="trading-key-grid">
                    <button class="btn outline ta-clear-trading" type="button">Revoke Trading Key</button>
                    <button class="btn outline ta-clear-fee" type="button">Revoke Fee Key</button>
               </div>`;
        const keyStatusHtml = privileged
            ? `<div class="profile-key-status-row profile-key-status-row-single" style="margin-top:8px;">
                    <span class="key-indicator${!acct.has_trading_session_key ? '' : acct.trading_key_expired ? ' expired' : ' ok'}">${!acct.has_trading_session_key ? 'Trading key not set' : acct.trading_key_expired ? 'Trading key expired' : 'Trading key set'}</span>
               </div>`
            : `<div class="profile-key-status-row" style="margin-top:8px;">
                    <span class="key-indicator${!acct.has_trading_session_key ? '' : acct.trading_key_expired ? ' expired' : ' ok'}">${!acct.has_trading_session_key ? 'Trading key not set' : acct.trading_key_expired ? 'Trading key expired' : 'Trading key set'}</span>
                    <span class="key-indicator${!acct.has_fee_session_key ? '' : acct.fee_key_expired ? ' expired' : ' ok'}">${!acct.has_fee_session_key ? 'Fee key not set' : acct.fee_key_expired ? 'Fee key expired' : 'Fee key set'}</span>
               </div>`;
        const keyMetaHtml = privileged
            ? `<div class="trading-account-meta trading-account-meta-single">
                    <span>Trading Expires: ${fmtMetaDate(acct.trading_session_expires_at)}</span>
               </div>`
            : `<div class="trading-account-meta">
                    <span>Trading Expires: ${fmtMetaDate(acct.trading_session_expires_at)}</span>
                    <span>Fee Expires: ${fmtMetaDate(acct.fee_session_expires_at)}</span>
               </div>`;
        const el = document.createElement('div');
        el.className = 'trading-account-item';
        el.dataset.name = acct.name;
        el.dataset.privileged = privileged ? '1' : '0';
        const sigType = Number.isInteger(Number(acct.signature_type)) ? Number(acct.signature_type) : 2;
        const hasUserRecord = !!acct.has_user_record;
        const userEnabled = !!acct.user_enabled;
        const adminEnabled = !!acct.admin_enabled;
        const effectiveEnabled = !!acct.enabled;
        el.innerHTML = `
            <div class="trading-account-head">
                <div class="ta-account-identity">
                    <span class="ta-avatar" aria-hidden="true">${String(acct.name || '?').trim().charAt(0).toUpperCase()}</span>
                    <div><strong>${acct.name || ''}</strong><span class="ta-account-subtitle">Execution account</span></div>
                </div>
                <div class="ta-head-actions">
                    <span class="ta-state-chip ${effectiveEnabled ? 'is-live' : 'is-paused'}">${effectiveEnabled ? 'Ready' : 'Paused'}</span>
                    <button class="ta-drag-handle" type="button" aria-label="Drag to reorder"><span aria-hidden="true">⠿</span> Reorder</button>
                </div>
            </div>
            <div class="ta-account-body">
                <section class="ta-access-panel">
                    <div class="ta-section-heading"><span>Trading access</span><small>User: ${userEnabled ? 'enabled' : 'paused'}</small></div>
                    <div class="ta-enabled-switch-row">
                        <label class="switch ta-switch admin-switch">
                            <input type="checkbox" class="ta-admin-enabled" ${acct.admin_enabled ? 'checked' : ''} />
                            <span class="slider"></span>
                        </label>
                        <div><strong>Admin permission</strong><small>${adminEnabled ? 'Orders are permitted' : 'Orders are blocked'}</small></div>
                    </div>
                </section>
                <section class="ta-wallet-panel">
                    <div class="ta-section-heading"><span>Funder</span><small>Connected execution wallet</small></div>
                    <div class="ta-wallet-row">
                        <div class="ta-funder ta-funder-readonly" data-value="${acct.funder || ''}" title="${acct.funder || ''}">${acct.funder || 'Not set'}</div>
                        <button class="btn outline ta-connect-wallet" type="button">Connect Wallet</button>
                    </div>
                </section>
                <section class="ta-policy-panel">
                    <label class="ta-signature-field"><span class="ta-field-label">Signature Type</span>
                        <select class="input ta-signature-type">
                            <option value="0" ${sigType === 0 ? 'selected' : ''}>EOA Wallet (0)</option>
                            <option value="1" ${sigType === 1 ? 'selected' : ''}>Legacy Mode (1)</option>
                            <option value="2" ${sigType === 2 ? 'selected' : ''}>Browser Safe (2)</option>
                        </select>
                    </label>
                    <label class="ta-admin-role">
                        <input type="checkbox" class="ta-is-admin" ${acct.is_admin ? 'checked' : ''} ${hasUserRecord ? '' : 'disabled'} />
                        <span class="ta-role-check" aria-hidden="true"></span>
                        <span class="ta-role-copy"><strong>Admin Account</strong><small>${hasUserRecord ? 'Management access' : 'No linked user record'}</small></span>
                    </label>
                </section>
            </div>
            <div class="ta-key-panel">
                <div class="ta-key-summary">${keyStatusHtml}${keyMetaHtml}</div>
                ${keyControlsHtml}
            </div>
            <div class="ta-account-actions">
                <button class="btn emergency ta-delete-account" type="button">Delete Account</button>
                <button class="btn primary ta-save short" type="button">Save Account</button>
            </div>
        `;

        wireTradingAccountDnD(el);
        wireTradingAccountActions(el, acct);
        root.appendChild(el);
        enhanceSelect(el.querySelector('.ta-signature-type'));
    });
}

async function saveTradingAccountOrder(root){
    const ordered_names = Array.from(root.querySelectorAll('.trading-account-item')).map(el => el.dataset.name).filter(Boolean);
    const r = await fetch('/api/trading/accounts/reorder', {
        method: 'POST',
        headers: {'Content-Type':'application/json', ...authHeaders()},
        body: JSON.stringify({ordered_names}),
    });
    const js = await r.json().catch(()=>({}));
    if (!r.ok){ showToast(js.detail || 'Failed to save account order', 'error', 3200); return false; }
    return true;
}

function wireTradingAccountDnD(el){
    const root = $('#trading-accounts-list');
    const handle = el.querySelector('.ta-drag-handle');
    if (!root || !handle) return;

    el.setAttribute('draggable', 'false');
    let dragArmed = false;
    handle.addEventListener('mousedown', ()=>{
        dragArmed = true;
        el.setAttribute('draggable', 'true');
    });
    handle.addEventListener('mouseup', ()=>{
        if (!adminDraggingAccountEl){
            dragArmed = false;
            el.setAttribute('draggable', 'false');
        }
    });

    el.addEventListener('dragstart', (e)=>{
        if (!dragArmed){
            e.preventDefault();
            return;
        }
        adminDraggingAccountEl = el;
        adminLastDragClientY = Number.isFinite(e.clientY) ? e.clientY : null;
        dragArmed = false;
        el.classList.add('dragging-live');
        document.body.classList.add('no-select');
        if (e.dataTransfer){
            e.dataTransfer.effectAllowed = 'move';
            e.dataTransfer.setData('text/plain', el.dataset.name || '');
        }
    });

    el.addEventListener('dragend', async ()=>{
        dragArmed = false;
        el.setAttribute('draggable', 'false');
        if (!adminDraggingAccountEl) return;
        adminDraggingAccountEl = null;
        adminLastDragClientY = null;
        el.classList.remove('dragging-live');
        document.body.classList.remove('no-select');
        const names = Array.from(root.querySelectorAll('.trading-account-item')).map(n => n.dataset.name);
        tradingAccounts.sort((a, b) => names.indexOf(a.name) - names.indexOf(b.name));
        const ok = await saveTradingAccountOrder(root);
        if (ok) showToast('Account order saved', 'success', 1800);
    });

    if (root.dataset.dndBound === '1') return;
    root.dataset.dndBound = '1';

    root.addEventListener('dragover', (e)=>{
        const draggingEl = adminDraggingAccountEl;
        if (!draggingEl) return;
        e.preventDefault();
        const edge = 120;
        let dy = 0;
        if (e.clientY < edge) dy = -14;
        else if (e.clientY > (window.innerHeight - edge)) dy = 14;
        if (dy) window.scrollBy(0, dy);
        const target = e.target.closest('.trading-account-item');
        if (!target || target === draggingEl) return;
        const rect = target.getBoundingClientRect();
        const y = Number(e.clientY || 0);
        const prevY = adminLastDragClientY;
        const movingUp = Number.isFinite(prevY) && y < prevY;
        const movingDown = Number.isFinite(prevY) && y > prevY;
        adminLastDragClientY = y;
        let before;
        if (movingUp) before = (y <= (rect.bottom - 2));
        else if (movingDown) before = (y < (rect.top + 2));
        else before = (y < rect.top + (rect.height / 2));
        if (before) root.insertBefore(draggingEl, target);
        else root.insertBefore(draggingEl, target.nextSibling);
    });
}

function wireTradingAccountActions(el, acct){
    el.querySelector('.ta-connect-wallet')?.addEventListener('click', async ()=>{
        try{
            const wallet = await connectWalletAddress();
            const field = el.querySelector('.ta-funder-readonly');
            if (field){
                field.dataset.value = wallet.funder || '';
                field.textContent = wallet.funder || 'Not set';
                field.title = wallet.funder || '';
            }
            // Clear session keys — they belong to the previous wallet
            await Promise.allSettled([
                fetch(`/api/trading/accounts/${encodeURIComponent(acct.name)}/session-keys/trading`, { method: 'DELETE', headers: authHeaders() }),
                fetch(`/api/trading/accounts/${encodeURIComponent(acct.name)}/session-keys/fee`,     { method: 'DELETE', headers: authHeaders() }),
            ]);
            await loadAdminTradingAccounts();
            showToast(`Wallet connected. Session keys cleared — please regenerate them.`, 'warning', 4000);
        } catch (err) {
            showToast(err.message || 'Wallet connection failed', 'error', 3200);
        }
    });

    el.querySelector('.ta-save')?.addEventListener('click', async ()=>{
        const sigRaw = el.querySelector('.ta-signature-type')?.value;
        const sigVal = Number(sigRaw);
        const funderVal = (el.querySelector('.ta-funder-readonly')?.dataset?.value || '').trim();
        const adminRoleEl = el.querySelector('.ta-is-admin');
        const body = {
            enabled: !!el.querySelector('.ta-admin-enabled')?.checked,
            signature_type: Number.isInteger(sigVal) ? sigVal : 2,
        };
        if (adminRoleEl && !adminRoleEl.disabled){
            body.is_admin = !!adminRoleEl.checked;
        }
        if (funderVal) body.funder = funderVal;
        const r = await fetch(`/api/trading/accounts/${encodeURIComponent(acct.name)}`, {
            method: 'PATCH',
            headers: {'Content-Type':'application/json', ...authHeaders()},
            body: JSON.stringify(body),
        });
        const js = await r.json().catch(()=>({}));
        if (!r.ok){ showToast(js.detail || 'Failed to save account', 'error', 3200); return; }
        showToast(`Saved ${acct.name}`, 'success');
        await loadAdminTradingAccounts();
    });

    el.querySelector('.ta-clear-trading')?.addEventListener('click', async ()=>{
        const r = await fetch(`/api/trading/accounts/${encodeURIComponent(acct.name)}/session-keys/trading`, {
            method: 'DELETE',
            headers: authHeaders(),
        });
        const js = await r.json().catch(()=>({}));
        if (!r.ok){ showToast(js.detail || 'Failed to clear trading key', 'error', 3200); return; }
        showToast(`Trading key cleared: ${acct.name}`, 'success');
        await loadAdminTradingAccounts();
    });

    el.querySelector('.ta-clear-fee')?.addEventListener('click', async ()=>{
        const r = await fetch(`/api/trading/accounts/${encodeURIComponent(acct.name)}/session-keys/fee`, {
            method: 'DELETE',
            headers: authHeaders(),
        });
        const js = await r.json().catch(()=>({}));
        if (!r.ok){ showToast(js.detail || 'Failed to clear fee key', 'error', 3200); return; }
        showToast(`Fee key cleared: ${acct.name}`, 'success');
        await loadAdminTradingAccounts();
    });

    el.querySelector('.ta-delete-account')?.addEventListener('click', ()=>{
        const dlg = $('#dlg-admin-delete');
        if (!dlg) return;
        adminDeleteTargetName = acct.name || '';
        $('#admin-delete-target').textContent = `Account: ${adminDeleteTargetName}`;
        $('#admin-delete-hint').textContent = '';
        dlg.showModal();
    });
}

// Username edit via dialog
const dlgU = $('#dlg-uname');
let unameCheckTimer = null;
let unameCheckSeq = 0;

function setUnameHint(msg, color=''){
    const hint = $('#uname-hint');
    if (!hint) return;
    hint.textContent = msg || '';
    hint.style.color = color || '';
}

function setUnameSubmitDisabled(disabled){
    const btn = $('#uname-submit');
    if (!btn) return;
    btn.disabled = !!disabled;
}

async function validateUsernameLive(value){
    const candidate = String(value || '').trim();
    const current = String($('#pf-username-view')?.textContent || '').trim();
    if (!candidate){
        setUnameHint('Username must be at least 8 characters', '#ef4444');
        setUnameSubmitDisabled(true);
        return false;
    }
    if (candidate.length < 8){
        setUnameHint('Username must be at least 8 characters', '#ef4444');
        setUnameSubmitDisabled(true);
        return false;
    }
    if (!/^[A-Za-z0-9_]+$/.test(candidate)){
        setUnameHint('Username can only use letters, numbers, and underscores', '#ef4444');
        setUnameSubmitDisabled(true);
        return false;
    }
    if (candidate.toLowerCase() === current.toLowerCase()){
        setUnameHint('', '');
        setUnameSubmitDisabled(false);
        return true;
    }

    const seq = ++unameCheckSeq;
    setUnameHint('', '');
    setUnameSubmitDisabled(true);
    try{
        let r = await fetch(`/api/username-availability?username=${encodeURIComponent(candidate)}`, {
            headers: authHeaders(),
        });
        let js = await r.json().catch(()=>({}));
        // Compatibility fallback if server is still on previous route.
        if (r.status === 404){
            r = await fetch(`/api/user/username-availability?username=${encodeURIComponent(candidate)}`, {
                headers: authHeaders(),
            });
            js = await r.json().catch(()=>({}));
        }
        if (seq !== unameCheckSeq) return false;
        if (!r.ok){
            const detail = String(js.detail || '');
            const normalized = (detail === 'Not Found' || !detail)
                ? 'Username check service unavailable. Restart server and try again.'
                : detail;
            setUnameHint(normalized, '#ef4444');
            setUnameSubmitDisabled(true);
            return false;
        }
        if (js.available){
            if (js.is_current){
                setUnameHint('', '');
            } else {
                setUnameHint('Username available', '#22c55e');
            }
            setUnameSubmitDisabled(false);
            return true;
        }
        setUnameHint(String(js.reason || 'Username unavailable'), '#ef4444');
        setUnameSubmitDisabled(true);
        return false;
    } catch (_err){
        if (seq !== unameCheckSeq) return false;
        setUnameHint('Username check service unavailable. Try again.', '#ef4444');
        setUnameSubmitDisabled(true);
        return false;
    }
}

$('#pf-edit-username')?.addEventListener('click', ()=>{
    const input = $('#dlg-uname-input');
    input.value = $('#pf-username-view').textContent || '';
    setUnameHint('', '');
    setUnameSubmitDisabled(false);
    if (unameCheckTimer) clearTimeout(unameCheckTimer);
    unameCheckTimer = setTimeout(()=>{ validateUsernameLive(input.value); }, 0);
    dlgU.showModal();
});
$('#uname-cancel')?.addEventListener('click', ()=>{ try{ dlgU.close(); }catch(_){} });
$('#dlg-uname-input')?.addEventListener('input', (e)=>{
    if (unameCheckTimer) clearTimeout(unameCheckTimer);
    setUnameHint('', '');
    setUnameSubmitDisabled(true);
    unameCheckTimer = setTimeout(()=>{
        validateUsernameLive(e.target.value);
    }, 250);
});
$('#uname-submit')?.addEventListener('click', async (e)=>{
    e.preventDefault();
    const new_username = ($('#dlg-uname-input').value||'').trim();
    const okToSubmit = await validateUsernameLive(new_username);
    if (!okToSubmit){
        return;
    }
    const r = await fetch('/api/user/profile', {method:'POST', headers:{'Content-Type':'application/json', ...authHeaders()}, body: JSON.stringify({new_username})});
    const js = await r.json().catch(()=>({}));
    if (r.ok){
        $('#user-menu-name').textContent = new_username;
        $('#pf-username-view').textContent = new_username;
        setUnameHint('', '');
        setTimeout(()=>{ try{ dlgU.close(); }catch(_){} }, 200);
    } else {
        const detail = String(js.detail || 'Failed to update username');
        setUnameHint(detail, '#ef4444');
        setUnameSubmitDisabled(true);
    }
});
// Handle profile form submit (Save Settings)
$('#pf-settings-form')?.addEventListener('submit', async (ev)=>{
    ev.preventDefault();
    const abbr = ($('#pf-timezone').value || currentAbbrForIana(userTimezone));
    const timezone = US_TZ_ABBR_TO_IANA[abbr] || userTimezone;
    const body = {timezone};
    const resp = await fetch('/api/user/profile', {method:'POST', headers:{'Content-Type':'application/json', ...authHeaders()}, body: JSON.stringify(body)});
    userTimezone = timezone;
    if (resp.ok){ showToast('Settings saved', 'success'); }
    else {
        const err = await resp.json().catch(()=>({}));
        showToast(err.detail || 'Failed to save settings', 'error', 3200);
    }
});

$('#pf-tr-save-settings')?.addEventListener('click', async ()=>{
    const authMode = profileAuthModeValue();
    const privateKeyInput = ($('#pf-tr-private-key')?.value || '').trim();
    const body = {
        // Profile toggle controls user-level enable only.
        enabled: !!$('#pf-tr-enabled')?.checked,
        auth_mode: authMode,
    };
    const funder = profileFunderValue();
    const walletOwner = profileWalletOwnerValue();
    const safeAddress = profileSafeValue();
    if (authMode === 'private_key' && privateKeyInput){
        body.private_key = privateKeyInput;
    }
    if (authMode === 'private_key'){
        if (walletOwner) body.wallet_owner = walletOwner;
    } else if (funder){
        body.funder = funder;
    } else if (!walletOwner && !safeAddress) {
        body.clear_funder = true;
    }
    if (walletOwner) body.wallet_owner = walletOwner;
    if (authMode !== 'private_key' && safeAddress) body.safe_address = safeAddress;
    if (body.enabled) {
        if (!myTradingAccount?.admin_enabled){
            showToast('This account must be enabled first on the Admin page', 'error', 3200);
            return;
        }
        if (authMode === 'private_key'){
            if (!privateKeyInput && !myTradingAccount?.has_private_key){
                showToast('Wallet private key is required before enabling trading', 'error', 3200);
                return;
            }
        } else {
        const hasTrading = !!(myTradingAccount?.has_trading_session_key);
        const needsFee = !!(myTradingAccount?.requires_fee_key ?? true);
        const hasFee = !!(myTradingAccount?.has_fee_session_key);
        if (needsFee && !hasTrading && !hasFee){
            showToast('Trading and fee session keys are required before enabling trading', 'error', 3200);
            return;
        }
        if (!hasTrading){
            showToast('Trading session key is required before enabling trading', 'error', 3200);
            return;
        }
        if (needsFee && !hasFee){
            showToast('Fee session key is required before enabling trading', 'error', 3200);
            return;
        }
        }
    }
    const r = await updateMyTradingAccountRequest(body);
    const js = await r.json().catch(()=>({}));
    if (!r.ok){ showToast(js.detail || 'Failed to save trading settings', 'error', 3200); return; }
    showToast('Trading settings saved', 'success');
    await loadMyTradingAccount();
});

$('#pf-tr-auth-mode')?.addEventListener('change', ()=>{
    updateProfileAuthModeUi(myTradingAccount);
});

$('#pf-tr-enabled')?.addEventListener('change', updateProfileTradingToggleUi);

$('#pf-tr-connect-wallet')?.addEventListener('click', async ()=>{
    try{
        const privateMode = profileAuthModeValue() === 'private_key';
        const wallet = await connectWalletAddress({ requireSafe: !privateMode, forcePrompt: true, allowSavedSafeFallback: !privateMode });
        setReadonlyAddressField('#pf-tr-funder', wallet.eoa || '');
        setReadonlyAddressField('#pf-tr-safe', wallet.safe || '');
        const saved = await persistProfileFunderIfPresent({
            wallet_owner: wallet.eoa,
            safe_address: wallet.safe || wallet.funder,
            auth_mode: privateMode ? 'private_key' : 'session_key',
        });
        if (!saved.ok){
            showToast(saved.detail || 'Wallet connected but failed to save funder', 'error', 3200);
            return;
        }
        // Clear session keys — they belong to the previous wallet
        if (!privateMode){
            await Promise.allSettled([
            fetch('/api/user/trading-account/session-keys/trading', { method: 'DELETE', headers: authHeaders() }),
            fetch('/api/user/trading-account/session-keys/fee',     { method: 'DELETE', headers: authHeaders() }),
            ]);
        }
        await loadMyTradingAccount();
        if (privateMode){
            showToast('Wallet connected. Add the matching private key, then save.', 'success', 4000);
            return;
        }
        showToast(`Wallet connected. Session keys cleared — please regenerate them.`, 'warning', 4000);
    } catch (err) {
        showToast(err.message || 'Wallet connection failed', 'error', 3200);
    }
});

$('#pf-tr-clear-funder')?.addEventListener('click', async ()=>{
    setReadonlyAddressField('#pf-tr-funder', '');
    setReadonlyAddressField('#pf-tr-safe', '');
    showToast('Addresses cleared locally. Click Save Trading Settings to apply.', 'warning', 2600);
});

$('#pf-tr-clear-private-key')?.addEventListener('click', async ()=>{
    const r = await updateMyTradingAccountRequest({ clear_private_key: true });
    const js = await r.json().catch(()=>({}));
    if (!r.ok){ showToast(js.detail || 'Failed to clear private key', 'error', 3200); return; }
    const input = $('#pf-tr-private-key');
    if (input) input.value = '';
    showToast('Private key cleared', 'success');
    await loadMyTradingAccount();
});

$('#pf-tr-generate-trading')?.addEventListener('click', async ()=>{
    const saved = await persistProfileFunderIfPresent();
    if (!saved.ok){ showToast(saved.detail || 'Failed to save funder', 'error', 3200); return; }
    const r = await fetch('/api/user/trading-account/session-keys/trading/generate', {
        method: 'POST',
        headers: authHeaders(),
    });
    const js = await r.json().catch(()=>({}));
    if (!r.ok){ showToast(js.detail || 'Failed to set trading key', 'error', 3200); return; }
    showToast('Trading session key updated', 'success');
    await loadMyTradingAccount();
});

$('#pf-tr-generate-fee')?.addEventListener('click', async ()=>{
    const saved = await persistProfileFunderIfPresent();
    if (!saved.ok){ showToast(saved.detail || 'Failed to save funder', 'error', 3200); return; }
    const r = await fetch('/api/user/trading-account/session-keys/fee/generate', {
        method: 'POST',
        headers: authHeaders(),
    });
    const js = await r.json().catch(()=>({}));
    if (!r.ok){ showToast(js.detail || 'Failed to set fee key', 'error', 3200); return; }
    showToast('Fee session key updated', 'success');
    await loadMyTradingAccount();
});

$('#pf-tr-clear-trading')?.addEventListener('click', async ()=>{
    const r = await fetch('/api/user/trading-account/session-keys/trading', {
        method: 'DELETE',
        headers: authHeaders(),
    });
    const js = await r.json().catch(()=>({}));
    if (!r.ok){ showToast(js.detail || 'Failed to clear trading key', 'error', 3200); return; }
    showToast('Trading session key cleared', 'success');
    await loadMyTradingAccount();
});

$('#pf-tr-clear-fee')?.addEventListener('click', async ()=>{
    const r = await fetch('/api/user/trading-account/session-keys/fee', {
        method: 'DELETE',
        headers: authHeaders(),
    });
    const js = await r.json().catch(()=>({}));
    if (!r.ok){ showToast(js.detail || 'Failed to clear fee key', 'error', 3200); return; }
    showToast('Fee session key cleared', 'success');
    await loadMyTradingAccount();
});

// Change password dialog wiring
const dlgPass = $('#dlg-pass');
$('#pf-open-pass')?.addEventListener('click', ()=>{ $('#pass-hint').textContent=''; $('#dlg-old').value=''; $('#dlg-new').value=''; $('#dlg-pw-req').style.display='none'; dlgPass.showModal(); });
$('#pass-cancel')?.addEventListener('click', ()=>{ try{ dlgPass.close(); }catch(_){} });
const pwDlgInput = $('#dlg-new');
pwDlgInput?.addEventListener('input', ()=>{
    const v = pwDlgInput.value||''; const hasLen=v.length>=10, hasMax=v.length<=20, hasLet=/[A-Za-z]/.test(v), hasDig=/\d/.test(v);
    $('#dlg-pw-req').style.display = v.length? 'block':'none';
    const set=(s,o)=>{const el=$(`#dlg-pw-req li[data-rule="${s}"]`); if (el) el.classList.toggle('ok',!!o)}; set('len',hasLen); set('max',hasMax); set('letters',hasLet); set('digits',hasDig);
});
$('#pass-submit')?.addEventListener('click', async (e)=>{
    e.preventDefault();
    const old_password = ($('#dlg-old').value||''); const new_password = ($('#dlg-new').value||'');
    if (!old_password || !new_password) { $('#pass-hint').textContent='Please enter both current and new password.'; return; }
    const strong=/^(?=.*[A-Za-z])(?=.*\d).{10,20}$/.test(new_password); if(!strong){ $('#pass-hint').textContent='New password does not meet requirements (10-20 chars, letters and numbers).'; return; }
    const r = await fetch('/api/user/profile', {method:'POST', headers:{'Content-Type':'application/json', ...authHeaders()}, body: JSON.stringify({old_password,new_password})});
    const js = await r.json().catch(()=>({}));
    if (r.ok){ $('#pass-hint').textContent='Password changed.'; setTimeout(()=>{ try{ dlgPass.close(); }catch(_){} }, 300); }
    else{ $('#pass-hint').textContent= js.detail || 'Failed to change password.'; }
});

// Toggle eye buttons (change password dialog)
function wireToggle(btnSel, inputSel){
    const b = document.querySelector(btnSel); const i = document.querySelector(inputSel);
    if (!b || !i) return;
    const sync = ()=>{
        const isPw = i.getAttribute('type') === 'password';
        b.classList.toggle('is-revealed', !isPw);
        b.title = isPw ? 'Show password' : 'Hide password';
        b.setAttribute('aria-label', b.title);
    };
    sync();
    b.addEventListener('click', ()=>{
        const isPw = i.getAttribute('type') === 'password';
        i.setAttribute('type', isPw ? 'text' : 'password');
        sync();
    });
}
wireToggle('#dlg-old-toggle', '#dlg-old');
wireToggle('#dlg-new-toggle', '#dlg-new');

// Delete account functionality
async function loadProfileDeleteButton(){
    // Hide delete card for admin users
    const r = await fetch('/api/me', {headers: authHeaders()});
    if (r.ok) {
        const me = await r.json();
        const deleteCard = $('#delete-account-card');
        if (me.is_admin) {
            deleteCard.style.display = 'none';
        } else {
            deleteCard.style.display = 'block';
        }
    }
}

$('#pf-delete-account')?.addEventListener('click', ()=>{
    const dlgDelete = $('#dlg-delete-account');
    $('#delete-hint').textContent = '';
    $('#delete-confirm-username').value = '';
    $('#delete-confirm').disabled = true;
    dlgDelete.showModal();
});

$('#delete-cancel')?.addEventListener('click', ()=>{
    try{ $('#dlg-delete-account').close(); } catch(_){}
});

$('#delete-confirm-username')?.addEventListener('input', ()=>{
    const currentUsername = $('#pf-username-view').textContent || '';
    const enteredUsername = $('#delete-confirm-username').value || '';
    const confirmBtn = $('#delete-confirm');
    confirmBtn.disabled = (enteredUsername !== currentUsername);
});

$('#delete-confirm')?.addEventListener('click', async (e)=>{
    e.preventDefault();
    const currentUsername = $('#pf-username-view').textContent || '';
    const enteredUsername = $('#delete-confirm-username').value || '';

    if (enteredUsername !== currentUsername) {
        $('#delete-hint').textContent = 'Username does not match.';
        return;
    }

    try {
        $('#dlg-delete-account').close();
        const response = await fetch('/api/user/delete', {method:'DELETE', headers: authHeaders()});
        if (response.ok) {
            alert('Account deleted successfully. You will be logged out.');
            doLogout();
        } else {
            const error = await response.json().catch(()=>({}));
            alert('Failed to delete account: ' + (error.detail || 'Unknown error'));
        }
    } catch (error) {
        alert('Error: ' + error.message);
    }
});

// Add backdrop close functionality for delete account dialog
const dlgDeleteAccount = $('#dlg-delete-account');
let deleteBackdropDown = false;
dlgDeleteAccount.addEventListener('mousedown', (e)=>{ deleteBackdropDown = (e.target === dlgDeleteAccount); });
dlgDeleteAccount.addEventListener('click', (e)=>{ if (e.target === dlgDeleteAccount && deleteBackdropDown) { try{ dlgDeleteAccount.close(); }catch(_){} } deleteBackdropDown=false; });
dlgDeleteAccount.addEventListener('cancel', (e)=>{ e.preventDefault(); try{ dlgDeleteAccount.close(); }catch(_){} });

// Admin delete-trading-account dialog
$('#admin-delete-cancel')?.addEventListener('click', ()=>{
    adminDeleteTargetName = '';
    try{ $('#dlg-admin-delete').close(); } catch(_){}
});

$('#admin-delete-confirm')?.addEventListener('click', async (e)=>{
    e.preventDefault();
    const target = String(adminDeleteTargetName || '').trim();
    if (!target){
        $('#admin-delete-hint').textContent = 'No account selected.';
        return;
    }
    try{
        const dlg = $('#dlg-admin-delete');
        if (dlg) dlg.close();
        const r = await fetch(`/api/trading/accounts/${encodeURIComponent(target)}`, {
            method: 'DELETE',
            headers: authHeaders(),
        });
        const js = await r.json().catch(()=>({}));
        if (!r.ok){
            showToast(js.detail || 'Failed to delete account', 'error', 3200);
            return;
        }
        showToast(`Deleted ${target}`, 'success');
        adminDeleteTargetName = '';
        await loadAdminTradingAccounts();
    } catch (err){
        showToast(err.message || 'Failed to delete account', 'error', 3200);
    }
});

const dlgAdminDelete = $('#dlg-admin-delete');
if (dlgAdminDelete){
    let adminDeleteBackdropDown = false;
    dlgAdminDelete.addEventListener('mousedown', (e)=>{ adminDeleteBackdropDown = (e.target === dlgAdminDelete); });
    dlgAdminDelete.addEventListener('click', (e)=>{ if (e.target === dlgAdminDelete && adminDeleteBackdropDown) { try{ dlgAdminDelete.close(); }catch(_){} } adminDeleteBackdropDown=false; });
    dlgAdminDelete.addEventListener('cancel', (e)=>{ e.preventDefault(); try{ dlgAdminDelete.close(); }catch(_){} });
}

// Load real statistics for about page
async function loadStats() {
    try {
        const response = await fetch('/api/stats');
        if (response.ok) {
            const stats = await response.json();
            // Only update if we get valid data
            if (stats.days_running !== undefined) {
                $('#st-days').textContent = stats.days_running;
            }
            if (stats.total_profit_with_base || stats.total_profit) {
                $('#st-profit').textContent = stats.total_profit_with_base || stats.total_profit;
            }
            if (stats.users !== undefined) {
                $('#st-users').textContent = stats.users;
            }
        }
    } catch (error) {
        console.error('Failed to load stats:', error);
        // Keep existing HTML values on error
    }
}

// Auto-refresh stats every 30 seconds
function startStatsAutoRefresh() {
    setInterval(() => {
        loadStats(); // Simple background update
    }, 30000);
    setInterval(() => {
        if (!isAuthed()) return;
        const selected = document.getElementById('user-account')?.value;
        const accountName = isAdmin && selected ? (selected === '*' ? currentUsername : selected) : null;
        loadUserStats(accountName, {preserve:true, force:true});
    }, 30000);
}

// Load stats on page load - just do a simple update
loadStats();

// Start auto-refresh
startStatsAutoRefresh();
