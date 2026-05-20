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

// ── Nav avatar dropdown ───────────────────────────────────────────────────────
(function () {
  const toggle   = document.getElementById('nav-avatar-toggle');
  const dropdown = document.getElementById('nav-avatar-dropdown');
  if (!toggle || !dropdown) return;

  toggle.addEventListener('click', function (e) {
    e.stopPropagation();
    const open = dropdown.classList.toggle('open');
    toggle.classList.toggle('open', open);
  });

  document.addEventListener('click', function (e) {
    if (!toggle.contains(e.target) && !dropdown.contains(e.target)) {
      dropdown.classList.remove('open');
      toggle.classList.remove('open');
    }
  });

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
      dropdown.classList.remove('open');
      toggle.classList.remove('open');
      mobileMenuClose();
    }
  });
})();

// ── Mobile hamburger menu ─────────────────────────────────────────────────────
(function () {
  const btn  = document.getElementById('nav-hamburger');
  const menu = document.getElementById('nav-mobile-menu');
  if (!btn || !menu) return;

  function mobileMenuOpen() {
    menu.classList.add('open');
    menu.setAttribute('aria-hidden', 'false');
    btn.setAttribute('aria-expanded', 'true');
    btn.classList.add('open');
  }

  window.mobileMenuClose = function () {
    menu.classList.remove('open');
    menu.setAttribute('aria-hidden', 'true');
    btn.setAttribute('aria-expanded', 'false');
    btn.classList.remove('open');
  };

  btn.addEventListener('click', function (e) {
    e.stopPropagation();
    if (menu.classList.contains('open')) {
      window.mobileMenuClose();
    } else {
      mobileMenuOpen();
      // Close avatar dropdown if open
      const avDrop = document.getElementById('nav-avatar-dropdown');
      const avToggle = document.getElementById('nav-avatar-toggle');
      if (avDrop) { avDrop.classList.remove('open'); }
      if (avToggle) { avToggle.classList.remove('open'); }
    }
  });

  document.addEventListener('click', function (e) {
    if (!btn.contains(e.target) && !menu.contains(e.target)) {
      window.mobileMenuClose();
    }
  });

  // Close menu when a nav link inside it is clicked (page navigates away)
  menu.querySelectorAll('a').forEach(function (link) {
    link.addEventListener('click', function () {
      window.mobileMenuClose();
    });
  });
})();

// ── Confirm-before-submit on data- attribute ─────────────────────────────────
document.addEventListener('submit', function (e) {
  const msg = e.target.dataset.confirm;
  if (msg && !confirm(msg)) {
    e.preventDefault();
  }
});
