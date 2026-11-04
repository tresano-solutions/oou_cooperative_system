(() => {
  const form = document.getElementById('task-feedback-form');
  if (!form) return;
  const status = document.getElementById('task-feedback-status');
  const buttons = form.querySelectorAll('button');
  document.getElementById('task-feedback-skip').addEventListener('click', () => {
    document.getElementById('task-feedback').hidden = true;
  });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const rating = event.submitter && event.submitter.value;
    if (!rating) return;
    buttons.forEach(button => { button.disabled = true; });
    status.textContent = 'Saving...';
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch(form.dataset.endpoint, {
        method: 'POST', credentials: 'same-origin', signal: controller.signal,
        headers: {'Content-Type': 'application/json', 'X-CSRFToken': form.elements.csrf_token.value},
        body: JSON.stringify({rating, token: form.elements.token.value})
      });
      if (!response.ok) throw new Error('Feedback unavailable');
      const result = await response.json();
      if (!result.ok) throw new Error('Feedback unavailable');
      form.querySelector('[role="group"]').remove();
      status.textContent = 'Thank you for your feedback.';
    } catch (_) {
      status.textContent = 'Could not save feedback. Retry or skip; your task is unaffected.';
      buttons.forEach(button => { button.disabled = false; });
    } finally { clearTimeout(timeout); }
  });
})();
