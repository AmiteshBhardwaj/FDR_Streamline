document.addEventListener('DOMContentLoaded', () => {
  const navButton = document.querySelector('[data-toggle-nav]');
  const sidebar = document.querySelector('#sidebar');
  const mobileLayout = window.matchMedia('(max-width: 800px)');
  const syncNav = () => {
    if (sidebar) sidebar.inert = mobileLayout.matches && !document.body.classList.contains('sidebar-open');
  };
  const closeNav = () => {
    document.body.classList.remove('sidebar-open');
    navButton?.setAttribute('aria-expanded', 'false');
    syncNav();
  };
  navButton?.addEventListener('click', () => {
    const expanded = document.body.classList.toggle('sidebar-open');
    navButton.setAttribute('aria-expanded', String(expanded));
    syncNav();
    if (expanded) sidebar?.querySelector('a')?.focus();
  });
  document.querySelector('[data-close-nav]')?.addEventListener('click', () => { closeNav(); navButton?.focus(); });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && document.body.classList.contains('sidebar-open')) { closeNav(); navButton?.focus(); }
  });
  mobileLayout.addEventListener('change', closeNav);
  syncNav();
  document.querySelectorAll('[data-dismiss]').forEach((button) => {
    button.addEventListener('click', () => button.closest('.flash-message')?.remove());
  });
  document.querySelectorAll('[data-toggle-password]').forEach((button) => {
    button.addEventListener('click', () => {
      const input = document.getElementById(button.dataset.togglePassword);
      if (!input) return;
      const reveal = input.type === 'password';
      input.type = reveal ? 'text' : 'password';
      button.textContent = reveal ? 'Hide password' : 'Show password';
      button.setAttribute('aria-pressed', String(reveal));
    });
  });
});
