/* Hub shared JavaScript */

// ── Local time (UTC stored on server → browser timezone) ─────────────────────
(function () {
  const FMTS = {
    "date-med":       { year: "numeric", month: "short", day: "numeric" },
    "date-long":      { year: "numeric", month: "long", day: "numeric" },
    "date-short":     { month: "short", day: "numeric" },
    "date-iso":       { year: "numeric", month: "2-digit", day: "2-digit" },
    "datetime-med":   { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" },
    "datetime-short": { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" },
    "datetime-full":  { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" },
    "datetime-iso":   { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false },
    "month-year":     { month: "long", year: "numeric" },
  };

  function parseUtc(isoUtc) {
    if (!isoUtc) return null;
    const s = isoUtc.endsWith("Z") || isoUtc.includes("+") ? isoUtc : isoUtc + "Z";
    const d = new Date(s);
    return isNaN(d.getTime()) ? null : d;
  }

  window.hubFormatUtcLocal = function (isoUtc, fmt) {
    const d = parseUtc(isoUtc);
    if (!d) return isoUtc || "—";
    const opts = FMTS[fmt] || FMTS["datetime-med"];
    if (fmt === "date-iso" || fmt === "datetime-iso") {
      return d.toLocaleString(undefined, opts);
    }
    return d.toLocaleString(undefined, { ...opts, hour12: undefined });
  };

  window.applyHubLocalTimes = function (root) {
    (root || document).querySelectorAll("time.local-time[datetime]").forEach((el) => {
      const fmt = el.dataset.fmt || "datetime-med";
      el.textContent = hubFormatUtcLocal(el.getAttribute("datetime"), fmt);
      el.title = hubFormatUtcLocal(el.getAttribute("datetime"), "datetime-full");
    });
    (root || document).querySelectorAll(".local-time-input[data-utc]").forEach((el) => {
      const fmt = el.dataset.fmt || "date-med";
      el.value = hubFormatUtcLocal(el.dataset.utc, fmt);
    });
  };

  document.addEventListener("DOMContentLoaded", () => applyHubLocalTimes());
})();

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
  } else if (el.id === 'new-key-display') {
    msg = 'API key copied — store it somewhere safe!';
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
    }
  });
})();

// ── Mobile navigation drawer ──────────────────────────────────────────────────
(function () {
  const btn     = document.getElementById('nav-hamburger');
  const drawer  = document.getElementById('nav-drawer');
  const overlay = document.getElementById('nav-drawer-overlay');
  const closeBtn = document.getElementById('nav-drawer-close');
  if (!btn || !drawer) return;

  function drawerOpen() {
    drawer.classList.add('open');
    drawer.setAttribute('aria-hidden', 'false');
    if (overlay) overlay.classList.add('open');
    btn.setAttribute('aria-expanded', 'true');
    btn.classList.add('open');
    document.body.style.overflow = 'hidden'; // prevent background scroll
  }

  window.drawerClose = function () {
    drawer.classList.remove('open');
    drawer.setAttribute('aria-hidden', 'true');
    if (overlay) overlay.classList.remove('open');
    btn.setAttribute('aria-expanded', 'false');
    btn.classList.remove('open');
    document.body.style.overflow = '';
  };

  btn.addEventListener('click', function (e) {
    e.stopPropagation();
    if (drawer.classList.contains('open')) {
      window.drawerClose();
    } else {
      drawerOpen();
      // Close avatar dropdown if it was open
      const avDrop   = document.getElementById('nav-avatar-dropdown');
      const avToggle = document.getElementById('nav-avatar-toggle');
      if (avDrop)   avDrop.classList.remove('open');
      if (avToggle) avToggle.classList.remove('open');
    }
  });

  // Close button inside drawer
  if (closeBtn) {
    closeBtn.addEventListener('click', function () { window.drawerClose(); });
  }

  // Click on overlay closes drawer
  if (overlay) {
    overlay.addEventListener('click', function () { window.drawerClose(); });
  }

  // Escape key closes drawer
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') window.drawerClose();
  });
})();

// ── Confirm-before-submit on data- attribute ─────────────────────────────────
document.addEventListener('submit', function (e) {
  const msg = e.target.dataset.confirm;
  if (msg && !confirm(msg)) {
    e.preventDefault();
  }
});
