/* Admin portal JavaScript */

// Auto-expand pending extension cards
document.addEventListener('DOMContentLoaded', function () {
  // Highlight pending extension rows
  document.querySelectorAll('.ext-card--pending').forEach(function (card) {
    card.style.boxShadow = '0 0 0 2px var(--color-primary)';
  });
});
