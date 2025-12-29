const $ = (q) => document.querySelector(q);
const $$ = (q) => Array.from(document.querySelectorAll(q));
const tokenKey = 'authToken';
let isAdmin = false;
let currentUsername = '';
// Default to PST if not set
let userTimezone = 'America/Los_Angeles';

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
    // Set loading state on all pages first
    $$('.page').forEach(el => {
        el.setAttribute('data-loading', 'true');
        el.classList.remove('active');
    });

    // Small delay to ensure stable layout before showing target
    setTimeout(() => {
        const target = $(page);
        if (target) {
            target.classList.add('active');
            target.removeAttribute('data-loading');
        }

        // Remove loading state from all other pages
        $$('.page').forEach(el => {
            if (el !== target) {
                el.removeAttribute('data-loading');
            }
        });
    }, 30);
}
// Simple routers for both path and hash (support deep links and older hashes)
function routeFromLocation(){
    const p = (location.pathname || '/').toLowerCase();
    if (p.startsWith('/admin')) { if (isAuthed() && isAdmin){ show('#page-admin'); loadAdmin(); } else { show('#page-home'); } closeMenus(); return; }
    if (p.startsWith('/profile')) { if (isAuthed()){ loadProfile(); show('#page-profile'); } else { show('#page-home'); } closeMenus(); return; }
    if (p.startsWith('/user') || p.startsWith('/home')) { if (isAuthed()){ show('#page-user'); loadUserData(); } else { show('#page-home'); } closeMenus(); return; }
    if (p.startsWith('/about')) { show('#page-home'); closeMenus(); return; }
    // Default routing: if authenticated, go to user page; otherwise home
    if (isAuthed()) {
        history.replaceState(null, '', '/user');
        show('#page-user');
        loadUserData();
    } else {
        show('#page-home');
    }
    closeMenus();
}
function routeFromHash(){
    const h = (location.hash || '').toLowerCase();
    if (!h) return false;
    if (h.startsWith('#/admin')) { if (isAuthed() && isAdmin){ show('#page-admin'); loadAdmin(); } else { show('#page-home'); } return true; }
    if (h.startsWith('#/profile')) { if (isAuthed()){ loadProfile(); show('#page-profile'); } else { show('#page-home'); } return true; }
    if (h.startsWith('#/user') || h.startsWith('#/home')) { if (isAuthed()){ show('#page-user'); loadUserData(); } else { show('#page-home'); } return true; }
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
            const strong = /^(?=.*[A-Za-z])(?=.*\d).{10,}$/.test(p);
            if (!strong) {
                // Show requirements, place focus, and use native validity bubble
                $('#pw-req').style.display = 'block';
                const pw = $('#auth-password');
                pw.setCustomValidity('Password must be 10+ characters and include letters and numbers.');
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
    btn.addEventListener('click', ()=>{
        const isPw = inp.getAttribute('type') === 'password';
        inp.setAttribute('type', isPw ? 'text' : 'password');
    });
}
})();

// Dynamic password requirements visibility and checks
const pwInput = $('#auth-password');
pwInput.addEventListener('input', ()=>{
const v = pwInput.value || '';
const isSignup = (dlg.dataset.mode === 'signup');
const hasLen = v.length >= 10;
const hasLet = /[A-Za-z]/.test(v);
const hasDig = /\d/.test(v);
// Only show requirements when signing up
$('#pw-req').style.display = (isSignup && v.length) ? 'block' : 'none';
if (isSignup){
    const set = (sel, ok)=>{ const el = $(`#pw-req li[data-rule="${sel}"]`); if (el) el.classList.toggle('ok', !!ok); };
    set('len', hasLen); set('letters', hasLet); set('digits', hasDig);
    // Clear or set custom validity live (signup only)
    if (hasLen && hasLet && hasDig) { pwInput.setCustomValidity(''); }
    else { pwInput.setCustomValidity('Password must be 10+ characters and include letters and numbers.'); }
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
    // Hide "Get Started" card on landing when logged in
    document.querySelector('.hero-right')?.classList.add('hidden');
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
                // If no changes selected, still send an empty object which the API should accept; otherwise, it’s a no-op
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

// Load the Polymarket small card using the official web component
async function updatePolymarketEmbed() {
    try {
        const host = document.getElementById('polymarket-embed-host');
        if (!host) return;

        // Prevent layout shifts by setting stable dimensions immediately
        host.style.width = '100%';
        host.style.maxWidth = '392px';
        host.style.height = '180px';
        host.style.minHeight = '180px';
        host.style.display = 'flex';
        host.style.alignItems = 'center';
        host.style.justifyContent = 'center';
        host.style.position = 'relative';
        host.style.overflow = 'hidden';
        host.style.margin = '0 auto';
        host.style.backgroundColor = '#1a1a1a';
        host.style.borderRadius = '8px';

        // Check if we already have an embed to prevent unnecessary reloads
        const existingEmbed = host.querySelector('polymarket-market-embed');
        if (existingEmbed) return;

        // Show skeleton loader that matches final dimensions
        host.innerHTML = `
            <div style="
                width: 100%;
                height: 100%;
                background: linear-gradient(90deg, #2a2a2a 25%, #3a3a3a 50%, #2a2a2a 75%);
                background-size: 200% 100%;
                animation: skeleton-loading 1.5s infinite;
                border-radius: 8px;
                display: flex;
                align-items: center;
                justify-content: center;
                color: #666;
                font-size: 14px;
            ">Loading market...</div>
            <style>
                @keyframes skeleton-loading {
                    0% { background-position: 200% 0; }
                    100% { background-position: -200% 0; }
                }
            </style>
        `;

        // Load the script if not already loaded
        if (!document.querySelector('script[src="https://embed.polymarket.com"]')) {
            const script = document.createElement('script');
            script.type = 'module';
            script.src = 'https://embed.polymarket.com';
            document.head.appendChild(script);
        }

        // Wait for the custom element to be defined
        if (!customElements.get('polymarket-market-embed')) {
            await customElements.whenDefined('polymarket-market-embed').catch(() => {});
        }

        // Get the market data
        const response = await fetch('/api/active-market', { headers: authHeaders() });
        if (!response.ok) {
            host.innerHTML = '<div style="color: #999; font-size: 14px; height: 100%; display: flex; align-items: center; justify-content: center;">Market unavailable</div>';
            return;
        }

        const data = await response.json();
        const slug = data.market?.slug;
        if (!slug) {
            host.innerHTML = '<div style="color: #999; font-size: 14px; height: 100%; display: flex; align-items: center; justify-content: center;">No market data</div>';
            return;
        }

        // Create the embed with exact same dimensions as container
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

        // Add accessibility attributes
        embed.setAttribute('role', 'img');
        embed.setAttribute('aria-label', 'Bitcoin prediction market chart from Polymarket');

        // Replace content without changing container dimensions
        host.innerHTML = '';
        host.appendChild(embed);

        // Fix any empty links that might be created by the embed
        setTimeout(() => {
            const emptyLinks = host.querySelectorAll('a:not([aria-label]):not([title]):empty, a[href]:not([aria-label]):not([title])');
            emptyLinks.forEach(link => {
                if (!link.textContent?.trim()) {
                    link.setAttribute('aria-label', 'View market on Polymarket');
                    link.setAttribute('title', 'View market on Polymarket');
                }
            });
        }, 1000);

    } catch (error) {
        console.error('Failed to load Polymarket embed:', error);
        const host = document.getElementById('polymarket-embed-host');
        if (host) {
            host.innerHTML = '<div style="color: #999; font-size: 14px; height: 100%; display: flex; align-items: center; justify-content: center;">Failed to load market</div>';
        }
    }
}

// User page
async function loadUserData(){
    // Set loading state to prevent layout shifts
    const userPage = document.getElementById('page-user');
    if (userPage) {
        userPage.setAttribute('data-loading', 'true');
    }

    try {
        // Start status and market embed in parallel so neither blocks the other
        const statsPromise = loadUserStats();
        const embedPromise = updatePolymarketEmbed();

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
        if (sel.options.length){ sel.value = sel.options[0].value; await loadTrades(sel.value); }
        sel.onchange = ()=> loadTrades(sel.value);
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

async function loadUserStats() {
    const statusIndicator = document.getElementById('status-indicator');
    const statusText = document.getElementById('status-text');
    const totalProfitElement = document.getElementById('user-total-profit');
    const currentStakeElement = document.getElementById('user-current-stake');

    console.log('🔄 loadUserStats() called');

    try {
        // Kick off both requests concurrently
        console.log('📡 Fetching user profile & summary in parallel...');
        const profilePromise = fetch('/api/user/profile', { headers: authHeaders() });
        const summaryPromise = fetch('/api/user/summary', { headers: authHeaders() });

        // Await profile first to set status ASAP
        const profileResponse = await profilePromise;
        console.log('📡 Profile response status:', profileResponse.status);
        if (!profileResponse.ok) throw new Error(`Profile API failed: ${profileResponse.status}`);
        const profile = await profileResponse.json();
        console.log('✅ Profile loaded:', profile);

        // Now handle summary without blocking status
        try {
            const summaryResponse = await summaryPromise;
            const summary = summaryResponse.ok ? await summaryResponse.json() : {};
            console.log('✅ Summary loaded:', summary);
            // Update total profit
            if (totalProfitElement && summary.total_profit !== undefined) {
                const profit = parseFloat(summary.total_profit) || 0;
                totalProfitElement.textContent = `$${profit.toFixed(2)}`;
                totalProfitElement.className = profit >= 0 ? 'positive' : 'negative';
                console.log('💰 Profit updated:', profit);
            }
            // Update current stake (position value)
            if (currentStakeElement && summary.mark) {
                const upValue = summary.mark.UP || 0;
                const downValue = summary.mark.DOWN || 0;
                const totalValue = upValue + downValue;
                currentStakeElement.textContent = `$${totalValue.toFixed(2)}`;
                console.log('📊 Stake updated:', totalValue);
            }
        } catch (e) {
            console.warn('Summary load failed or incomplete:', e);
        }

        // Update trading status based on user's pause setting
        if (statusIndicator && statusText) {
            const isPaused = profile.paused === true;
            console.log('🎯 User paused status:', isPaused, 'from profile:', profile.paused);

            if (isPaused) {
                statusIndicator.className = 'status-indicator paused';
                statusText.textContent = 'Paused';
                console.log('⏸️ Status set to Paused');
            } else {
                statusIndicator.className = 'status-indicator running';
                statusText.textContent = 'Active';
                console.log('▶️ Status set to Active');
            }
        } else {
            console.error('❌ Status elements not found');
        }

    } catch (error) {
        console.error('❌ Error loading user stats:', error);

        // Set fallback values with proper error handling
        if (totalProfitElement) totalProfitElement.textContent = '$0.00';
        if (currentStakeElement) currentStakeElement.textContent = '$0.00';

        // Keep default "Active" status on error
        console.log('🔄 Keeping default Active status due to error');
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
        const tdEv = document.createElement('td'); tdEv.textContent = mapEvent(r.event);
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
        userLiveTimer = setInterval(tick, 5000);
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
    sel.innerHTML = '';
    ['PST','MST','CST','EST'].forEach(ab=>{
        const opt=document.createElement('option'); opt.value=ab; opt.textContent=ab; sel.appendChild(opt);
    });
}
populateTimezones();

async function loadProfile(){
    const r = await fetch('/api/user/profile', {headers: authHeaders()});
    if (!r.ok) return;
    const js = await r.json();
    const uname = js.username || '';
    $('#pf-username-view').textContent = uname;
    $('#pf-paused').checked = !!js.paused;
    const abbr = currentAbbrForIana(js.timezone || userTimezone);
    const base = (abbr||'').replace('DT','ST');
    const sel = document.getElementById('pf-timezone');
    if (sel){ sel.value = ['PST','MST','CST','EST'].includes(base) ? base : 'PST'; }
    // Private key hint (do not expose full key)
    const pkInput = $('#pf-privkey');
    if (js.has_private_key){
        // Always show beginning: 0x + first 4 hex; if we only got an ending like "...8026",
        // we still show a generic 0x•••• to avoid implying exact tail.
        let hint = js.private_key_hint || '';
        let display = '0x' + '••••';
        // If the server provided an ellipsis (e.g., ...8026), don't infer prefix; mask instead
        if (!hint.includes('...')) {
            // Prefer an actual starting 0x prefix with at least 4 hex characters
            const mStart = hint.match(/^0x([0-9a-fA-F]{4,})/);
            if (mStart) {
                display = '0x' + mStart[1].slice(0,4);
            } else if (/^[0-9a-fA-F]{4,}/.test(hint)) {
                // Or a plain hex string starting at position 0
                display = '0x' + hint.slice(0,4);
            }
        }
        pkInput.placeholder = `Stored (${display})`;
        pkInput.value = '';
    } else {
        pkInput.placeholder = '0x… (never shared)';
        pkInput.value = '';
    }
    // Load delete button visibility
    await loadProfileDeleteButton();
}
// Username edit via dialog
const dlgU = $('#dlg-uname');
$('#pf-edit-username')?.addEventListener('click', ()=>{
    $('#uname-hint').textContent='';
    $('#dlg-uname-input').value = $('#pf-username-view').textContent || '';
    dlgU.showModal();
});
$('#uname-cancel')?.addEventListener('click', ()=>{ try{ dlgU.close(); }catch(_){} });
$('#uname-submit')?.addEventListener('click', async (e)=>{
    e.preventDefault();
    const new_username = ($('#dlg-uname-input').value||'').trim();
    if (!new_username) { $('#uname-hint').textContent = 'Enter a username.'; return; }
    const r = await fetch('/api/user/profile', {method:'POST', headers:{'Content-Type':'application/json', ...authHeaders()}, body: JSON.stringify({new_username})});
    const js = await r.json().catch(()=>({}));
    if (r.ok){
        $('#user-menu-name').textContent = new_username;
        $('#pf-username-view').textContent = new_username;
        setTimeout(()=>{ try{ dlgU.close(); }catch(_){} }, 200);
    } else {
        $('#uname-hint').textContent = js.detail || 'Failed to update username';
    }
});
// Handle profile form submit (Save Settings)
$('#pf-settings-form')?.addEventListener('submit', async (ev)=>{
    ev.preventDefault();
    const paused = !!$('#pf-paused').checked;
    const abbr = ($('#pf-timezone').value || currentAbbrForIana(userTimezone));
    const timezone = US_TZ_ABBR_TO_IANA[abbr] || userTimezone;
    const pkInput = $('#pf-privkey');
    const pkVal = (pkInput.value||'').trim();
    const clear_private_key = (pkInput.dataset.cleared === '1') ? true : undefined;
    const body = {paused, timezone};
    if (pkVal) body.private_key = pkVal;
    if (clear_private_key) body.clear_private_key = true;
    const resp = await fetch('/api/user/profile', {method:'POST', headers:{'Content-Type':'application/json', ...authHeaders()}, body: JSON.stringify(body)});
    userTimezone = timezone; // internal uses IANA
    if (resp.ok){
        showToast('Settings saved', 'success');
    } else {
        const err = await resp.json().catch(()=>({}));
        showToast(err.detail || 'Failed to save settings', 'error', 3200);
    }
});
$('#pf-clear-privkey')?.addEventListener('click', ()=>{ const i=$('#pf-privkey'); i.value=''; i.dataset.cleared='1'; i.placeholder='Will clear on Save'; });
// Toggle eye buttons (profile privkey)
$('#pf-toggle-privkey')?.addEventListener('click', ()=>{
    const i = $('#pf-privkey'); if (!i) return;
    const isPw = i.getAttribute('type') === 'password';
    i.setAttribute('type', isPw ? 'text' : 'password');
});
// Change password dialog wiring
const dlgPass = $('#dlg-pass');
$('#pf-open-pass')?.addEventListener('click', ()=>{ $('#pass-hint').textContent=''; $('#dlg-old').value=''; $('#dlg-new').value=''; $('#dlg-pw-req').style.display='none'; dlgPass.showModal(); });
$('#pass-cancel')?.addEventListener('click', ()=>{ try{ dlgPass.close(); }catch(_){} });
const pwDlgInput = $('#dlg-new');
pwDlgInput?.addEventListener('input', ()=>{
    const v = pwDlgInput.value||''; const hasLen=v.length>=10, hasLet=/[A-Za-z]/.test(v), hasDig=/\d/.test(v);
    $('#dlg-pw-req').style.display = v.length? 'block':'none';
    const set=(s,o)=>{const el=$(`#dlg-pw-req li[data-rule="${s}"]`); if (el) el.classList.toggle('ok',!!o)}; set('len',hasLen); set('letters',hasLet); set('digits',hasDig);
});
$('#pass-submit')?.addEventListener('click', async (e)=>{
    e.preventDefault();
    const old_password = ($('#dlg-old').value||''); const new_password = ($('#dlg-new').value||'');
    if (!old_password || !new_password) { $('#pass-hint').textContent='Please enter both current and new password.'; return; }
    const strong=/^(?=.*[A-Za-z])(?=.*\d).{10,}$/.test(new_password); if(!strong){ $('#pass-hint').textContent='New password does not meet requirements.'; return; }
    const r = await fetch('/api/user/profile', {method:'POST', headers:{'Content-Type':'application/json', ...authHeaders()}, body: JSON.stringify({old_password,new_password})});
    const js = await r.json().catch(()=>({}));
    if (r.ok){ $('#pass-hint').textContent='Password changed.'; setTimeout(()=>{ try{ dlgPass.close(); }catch(_){} }, 300); }
    else{ $('#pass-hint').textContent= js.detail || 'Failed to change password.'; }
});

// Toggle eye buttons (change password dialog)
function wireToggle(btnSel, inputSel){
    const b = document.querySelector(btnSel); const i = document.querySelector(inputSel);
    if (!b || !i) return;
    b.addEventListener('click', ()=>{
        const isPw = i.getAttribute('type') === 'password';
        i.setAttribute('type', isPw ? 'text' : 'password');
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
}

// Load stats on page load - just do a simple update
loadStats();

// Start auto-refresh
startStatsAutoRefresh();

