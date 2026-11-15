document.querySelectorAll('[data-save-form]').forEach(form => {
  const submitButton = form.querySelector('button[type="submit"]');
  if (submitButton) submitButton.dataset.originalLabel = submitButton.textContent;
  form.addEventListener('submit', () => {
    const button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    button.textContent = 'Сохранение…';
  });
});
window.addEventListener('pageshow', () => {
  document.querySelectorAll('[data-save-form] button[type="submit"]').forEach(button => {
    button.disabled = false;
    button.textContent = button.dataset.originalLabel || 'Сохранить';
  });
});
