/* Admin portal JavaScript */

document.addEventListener('DOMContentLoaded', function () {
  document.querySelectorAll('.ext-card--pending').forEach(function (card) {
    card.style.boxShadow = '0 0 0 2px var(--color-primary)';
  });

  var sidebar  = document.getElementById('admin-sidebar');
  var overlay  = document.getElementById('admin-sidebar-overlay');
  var toggle   = document.getElementById('admin-sidebar-toggle');
  if (!sidebar || !overlay || !toggle) return;

  function openSidebar() {
    sidebar.classList.add('open');
    overlay.classList.add('open');
    overlay.setAttribute('aria-hidden', 'false');
    toggle.setAttribute('aria-expanded', 'true');
    document.body.style.overflow = 'hidden';
  }

  function closeSidebar() {
    sidebar.classList.remove('open');
    overlay.classList.remove('open');
    overlay.setAttribute('aria-hidden', 'true');
    toggle.setAttribute('aria-expanded', 'false');
    document.body.style.overflow = '';
  }

  toggle.addEventListener('click', function () {
    if (sidebar.classList.contains('open')) closeSidebar();
    else openSidebar();
  });

  overlay.addEventListener('click', closeSidebar);

  sidebar.querySelectorAll('.admin-nav__link').forEach(function (link) {
    link.addEventListener('click', function () {
      if (window.matchMedia('(max-width: 900px)').matches) closeSidebar();
    });
  });

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') closeSidebar();
  });

  window.addEventListener('resize', function () {
    if (window.matchMedia('(min-width: 901px)').matches) closeSidebar();
  });
});
