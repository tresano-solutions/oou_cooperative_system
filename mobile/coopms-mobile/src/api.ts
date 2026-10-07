import * as SecureStore from 'expo-secure-store';
import { HQ_API_BASE, codeToBaseUrl, getApiBase } from './config';
import type { CtasPayload, DashboardPayload, Loan, LoanOptionsPayload, MobileNotification, PayInPayload, SavingRow } from './types';

const TOKEN_KEY = 'coopms.mobile.token';

type ApiOptions = {
  method?: 'GET' | 'POST' | 'PATCH';
  body?: Record<string, unknown>;
  token?: string | null;
  timeoutMs?: number;
};

const TENANT_CODE_ALIASES: Record<string, string> = {
  oou: 'ooucoop',
  ooucoop: 'ooucoop',
  ooucooperative: 'ooucoop',
  ooucooperativecms: 'ooucoop',
  smt: 'smtcoop',
  smtcoop: 'smtcoop',
  smtcooperative: 'smtcoop',
  hq: 'hq'
};

export class ApiError extends Error {
  status: number;
  code: string;

  constructor(message: string, status: number, code = '') {
    super(message);
    this.status = status;
    this.code = code;
  }
}

async function parseResponse(response: Response) {
  return response.json().catch(() => ({} as Record<string, unknown>));
}

async function fetchWithTimeout(url: string, init: RequestInit = {}, timeoutMs = 15000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...init, signal: controller.signal });
  } catch (error) {
    if (error instanceof Error && error.name === 'AbortError') {
      throw new ApiError('Connection timed out. Check your internet connection and try again.', 0);
    }
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

async function request<T>(path: string, options: ApiOptions = {}): Promise<T> {
  const headers: Record<string, string> = {
    Accept: 'application/json'
  };
  if (options.body) headers['Content-Type'] = 'application/json';
  if (options.token) headers.Authorization = `Bearer ${options.token}`;

  const base = getApiBase();
  if (!base) throw new ApiError('No cooperative selected.', 0);

  const response = await fetchWithTimeout(`${base}${path}`, {
    method: options.method || 'GET',
    headers,
    body: options.body ? JSON.stringify(options.body) : undefined
  }, options.timeoutMs);

  const payload = await parseResponse(response);
  if (!response.ok || payload.success === false) {
    throw new ApiError((payload.error as string) || `Request failed (${response.status})`, response.status, (payload.code as string) || '');
  }
  return payload as T;
}

/** Look up a cooperative by code/domain via its public tenant endpoint.
 *  Returns the resolved API base + display name so the app can target the right
 *  backend and brand its login screen. Throws ApiError with a friendly message. */
export async function resolveTenant(code: string): Promise<{ base: string; coopName: string; logo: string }> {
  const rawCode = (code || '').trim().toLowerCase();
  const compactCode = rawCode.replace(/[^a-z0-9.-]/g, '');
  const cleanCode = TENANT_CODE_ALIASES[compactCode] || compactCode;
  if (!cleanCode) throw new ApiError('Enter your cooperative code.', 0);
  try {
    const response = await fetchWithTimeout(
      `${HQ_API_BASE}/api/mobile/v1/tenants/resolve?code=${encodeURIComponent(cleanCode)}`,
      { headers: { Accept: 'application/json' } }
    );
    const payload = await parseResponse(response);
    const tenant = payload.tenant as Record<string, unknown> | undefined;
    if (response.ok && payload.success === true && tenant?.base_url) {
      return {
        base: String(tenant.base_url).replace(/\/+$/, ''),
        coopName: String(tenant.coop_name || tenant.name || 'Cooperative'),
        logo: String(tenant.logo || '')
      };
    }
  } catch {
    // Fall back to direct tenant probing below so local/dev testing still works.
  }

  const base = codeToBaseUrl(cleanCode);
  let response: Response;
  try {
    response = await fetchWithTimeout(`${base}/api/mobile/v1/tenant`, { headers: { Accept: 'application/json' } });
  } catch {
    throw new ApiError(`Could not reach ${base}. Check the cooperative code, your internet connection, and that the tenant is deployed.`, 0);
  }
  const payload = await parseResponse(response);
  if (!response.ok || payload.success !== true) {
    throw new ApiError('Cooperative not found — check the code with your society.', response.status);
  }
  return {
    base,
    coopName: (payload.coop_name as string) || 'Cooperative',
    logo: (payload.logo as string) || ''
  };
}

export async function saveToken(token: string) {
  await SecureStore.setItemAsync(TOKEN_KEY, token);
}

export async function loadToken() {
  return SecureStore.getItemAsync(TOKEN_KEY);
}

export async function clearToken() {
  await SecureStore.deleteItemAsync(TOKEN_KEY);
}

export async function login(username: string, password: string, otp = '') {
  const payload = await request<{ success: boolean; token: string; user: unknown }>('/api/mobile/login', {
    method: 'POST',
    body: otp ? { username, password, otp } : { username, password }
  });
  await saveToken(payload.token);
  return payload;
}

export async function requestPasswordReset(identifier: string) {
  return request<{ success: boolean; message: string }>('/api/mobile/v1/auth/forgot-password', {
    method: 'POST',
    body: { identifier }
  });
}

export async function changePassword(token: string, currentPassword: string, newPassword: string, confirmPassword: string) {
  return request<{ success: boolean; message: string }>('/api/mobile/v1/auth/change-password', {
    method: 'POST',
    token,
    body: {
      current_password: currentPassword,
      new_password: newPassword,
      confirm_password: confirmPassword
    }
  });
}

export async function getDashboard(token: string) {
  return request<DashboardPayload>('/api/mobile/v1/dashboard', { token });
}

export async function getProfile(token: string) {
  return request<{ success: boolean; profile: Record<string, string>; profile_completion: unknown }>(
    '/api/mobile/v1/profile',
    { token }
  );
}

export async function updateProfile(token: string, profile: Record<string, string>) {
  return request<{ success: boolean; member: DashboardPayload['member'] }>('/api/mobile/v1/profile', {
    method: 'PATCH',
    token,
    body: profile
  });
}

export async function getSavings(token: string) {
  return request<{ success: boolean; balance: number; rows: SavingRow[] }>('/api/mobile/v1/savings', { token });
}

export async function getLoans(token: string) {
  return request<{ success: boolean; loans: Loan[] }>('/api/mobile/v1/loans', { token });
}

export async function getCtas(token: string) {
  return request<CtasPayload>('/api/mobile/v1/ctas', { token });
}

export async function applyCtas(token: string, input: {
  cycle_id: number; target_amount: number; tenure_months: number;
  terms_accepted: boolean; signature_name: string;
}) {
  return request<{ success: boolean; monthly_deduction: number; admin_fee: number; eligible: boolean }>(
    '/api/mobile/v1/ctas/apply', { method: 'POST', token, body: input });
}

export async function getLoanOptions(token: string) {
  return request<LoanOptionsPayload>('/api/mobile/v1/loans/options', { token });
}

export async function previewLoanSchedule(token: string, input: { amount: number; purpose: string; tenure: number }) {
  return request<{
    success: boolean;
    amount: number;
    purpose: string;
    tenure: number;
    interest_rate: number;
    interest_method: string;
    monthly_payment: number;
    total_repayment: number;
    total_interest: number;
    schedule: Loan['schedule'];
  }>('/api/mobile/v1/loans/schedule-preview', {
    method: 'POST',
    token,
    body: input
  });
}

export async function applyForLoan(token: string, input: Record<string, unknown>) {
  return request<{ success: boolean; loan: Loan }>('/api/mobile/v1/loans/apply', {
    method: 'POST',
    token,
    body: input
  });
}

export async function getLoanDetail(token: string, loanId: number) {
  return request<{ success: boolean; loan: Loan }>(`/api/mobile/v1/loans/${loanId}`, { token });
}

export async function withdrawLoan(token: string, loanId: number, reason: string) {
  return request<{ success: boolean; loan: Loan }>(`/api/mobile/v1/loans/${loanId}/withdraw`, {
    method: 'POST',
    token,
    body: { reason }
  });
}

export async function getNotifications(token: string) {
  return request<{ success: boolean; notifications: MobileNotification[] }>('/api/mobile/v1/notifications', { token });
}

export async function markAllNotificationsRead(token: string) {
  return request<{ success: boolean }>('/api/mobile/v1/notifications/mark-all-read', {
    method: 'POST',
    token
  });
}

export async function registerDevice(token: string, pushToken: string, platform: string, deviceName: string) {
  return request<{ success: boolean; device_id: number }>('/api/mobile/v1/devices', {
    method: 'POST',
    token,
    body: {
      push_token: pushToken,
      platform,
      device_name: deviceName
    }
  });
}

/** The member's own account number, and what they said transfers pay for.
 *  Returns enabled:false when the cooperative does not issue account numbers,
 *  so the screen hides the section instead of showing an error. */
export async function getPayIn(token: string) {
  return request<PayInPayload>('/api/mobile/v1/pay-in', { token });
}

export async function setPayInPreference(token: string, preference: string) {
  return request<PayInPayload>('/api/mobile/v1/pay-in', {
    method: 'PATCH',
    token,
    body: { preference }
  });
}
