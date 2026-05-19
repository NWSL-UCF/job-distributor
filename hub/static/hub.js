/* Hub shared JavaScript */

// ── Toast notifications ──────────────────────────────────────────────────────
(function () {
  const el = document.createElement('div');
  el.id = 'toast';
  document.body.appendChild(el);

  window.showToast = function (msg, duration = 2200) {
    el.textContent = msg;
    el.classList.add('show');
    clearTimeout(window._toastTimer);
    window._toastTimer = setTimeout(() => el.classList.remove('show'), duration);
  };
})();

// ── Copy helpers (fallback for non-secure contexts) ──────────────────────────
window.copyText = function (id) {
  const el = document.getElementById(id);
  if (!el) return;
  const text = el.value !== undefined ? el.value : el.textContent;
  let msg = 'Copied!';
  if (el.classList && el.classList.contains('endpoint-item__input')) {
    msg = el.id === 'api-key-full' ? 'API key copied' : 'URL copied';
  } else if (el.type === 'hidden' && el.id && el.id.startsWith('dash-url-')) {
    msg = 'Dashboard URL copied';
  } else if (el.id === 'url-dashboard') {
    msg = 'Dashboard URL copied';
  }
  if (navigator.clipboard) {
    navigator.clipboard.writeText(text).then(() => showToast(msg));
  } else {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
    showToast(msg);
  }
};

window.copyFrpc = function () {
  const el = document.getElementById('frpc-config');
  if (!el) return;
  const text = el.textContent;
  if (navigator.clipboard) {
    navigator.clipboard.writeText(text).then(() => showToast('frpc.ini copied!'));
  }
};

// ── Confirm-before-submit on data- attribute ─────────────────────────────────
document.addEventListener('submit', function (e) {
  const msg = e.target.dataset.confirm;
  if (msg && !confirm(msg)) {
    e.preventDefault();
  }
});
