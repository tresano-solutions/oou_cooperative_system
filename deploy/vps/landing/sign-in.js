'use strict';

const form = document.getElementById('cooperative-sign-in');
const input = document.getElementById('cooperative-code');
const statusMessage = document.getElementById('status');
const button = form.querySelector('button');
let busy = false;

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (busy) return;
  const code = input.value.trim().toLowerCase();
  if (!/^[a-z0-9][a-z0-9-]{1,30}$/.test(code)) {
    statusMessage.textContent = 'Enter the cooperative code provided by your society.';
    input.focus();
    return;
  }
  busy = true;
  button.disabled = true;
  button.textContent = 'Checking cooperative...';
  statusMessage.textContent = '';
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 12000);
  try {
    const response = await fetch('/api/mobile/v1/tenants/resolve?code=' + encodeURIComponent(code), {
      signal: controller.signal, cache: 'no-store', credentials: 'omit', referrerPolicy: 'no-referrer'
    });
    if (!response.ok) {
      if (response.status === 400 || response.status === 404) {
        statusMessage.textContent = 'Unable to find your cooperative. Check your code with your administrator.';
        return;
      }
      throw new Error('Lookup unavailable');
    }
    const data = await response.json();
    if (!data.success || !data.tenant || data.tenant.code !== code) throw new Error('Invalid response');
    const destination = new URL(data.tenant.base_url);
    // Never allow an API response to redirect to an external or insecure site.
    if (destination.protocol !== 'https:' || destination.username || destination.password ||
        destination.port || !/^[a-z0-9][a-z0-9-]*\.cooperativems\.com$/.test(destination.hostname)) {
      throw new Error('Invalid destination');
    }
    window.location.assign(destination.origin + '/login');
  } catch (_) {
    statusMessage.textContent = 'We could not connect right now. Please try again shortly.';
  } finally {
    clearTimeout(timeout);
    busy = false;
    button.disabled = false;
    button.textContent = 'Continue to sign in';
  }
});
