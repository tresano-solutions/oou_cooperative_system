import { Ionicons } from '@expo/vector-icons';
import * as Clipboard from 'expo-clipboard';
import Constants from 'expo-constants';
import * as Notifications from 'expo-notifications';
import * as Updates from 'expo-updates';
import React, { Component, type ErrorInfo, type ReactNode, useEffect, useMemo, useState } from 'react';
import {
  ActivityIndicator,
  Alert,
  KeyboardAvoidingView,
  Linking,
  Platform,
  Pressable,
  SafeAreaView,
  ScrollView,
  StatusBar,
  StyleSheet,
  Text,
  TextInput,
  View
} from 'react-native';

import {
  ApiError,
  applyCtas,
  applyForLoan,
  changePassword,
  clearToken,
  getCtas,
  getDashboard,
  getLoanDetail,
  getLoanOptions,
  getNotifications,
  getPayIn,
  getProfile,
  getSavings,
  loadToken,
  login,
  markAllNotificationsRead,
  previewLoanSchedule,
  registerDevice,
  requestPasswordReset,
  resolveTenant,
  setPayInPreference,
  updateProfile,
  withdrawLoan
} from './src/api';
import { clearTenant, getApiBase, getCoopName, loadTenant, setTenant } from './src/config';
import type { CtasPayload, DashboardPayload, GuarantorOption, Loan, LoanOptionsPayload, MobileNotification, PayInPayload, SavingRow } from './src/types';

type Tab = 'home' | 'profile' | 'savings' | 'loans' | 'ctas' | 'notifications';

type FatalScreenProps = {
  title?: string;
  message: string;
  details?: string;
  onRetry?: () => void;
  secondaryLabel?: string;
  onSecondary?: () => void;
};

type AppErrorBoundaryState = {
  error: Error | null;
};

const NAVY = '#083574';
const BLUE = '#1450A3';
const YELLOW = '#F8B91E';
const BG = '#EEF3F8';
const INK = '#102033';
const BORDER = '#D9E2EE';
const MUTED = '#607086';
// Both app stores require a reachable privacy policy; Apple also expects a
// link inside the app itself.
const PRIVACY_URL = 'https://cooperativems.com/privacy';

/** What this phone is actually running. When a member reports a problem, this
 *  is the difference between guessing and knowing which build and which
 *  over-the-air update they are on. */
function buildStamp() {
  const version = Constants.expoConfig?.version || '1.0.0';
  if (Updates.isEmbeddedLaunch || !Updates.updateId) {
    return `Version ${version}`;
  }
  return `Version ${version} · update ${Updates.updateId.slice(0, 8)}`;
}

function money(value?: number) {
  return `NGN ${(value || 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function FatalScreen({ title = 'App could not start', message, details, onRetry,
                      secondaryLabel, onSecondary }: FatalScreenProps) {
  return (
    <SafeAreaView style={styles.fatalRoot}>
      <StatusBar barStyle="light-content" />
      <View style={styles.fatalCard}>
        <Text style={styles.fatalTitle}>{title}</Text>
        <Text style={styles.fatalMessage}>{message}</Text>
        {details ? <Text style={styles.fatalDetails}>{details}</Text> : null}
        {onRetry ? (
          <Pressable style={styles.primaryButton} onPress={onRetry}>
            <Text style={styles.primaryButtonText}>Try Again</Text>
          </Pressable>
        ) : null}
        {onSecondary && secondaryLabel ? (
          <Pressable style={({ pressed }) => [styles.fatalSecondary, pressed && styles.pressed]}
                     onPress={onSecondary}>
            <Text style={styles.fatalSecondaryText}>{secondaryLabel}</Text>
          </Pressable>
        ) : null}
      </View>
    </SafeAreaView>
  );
}

class AppErrorBoundary extends Component<{ children: ReactNode }, AppErrorBoundaryState> {
  state: AppErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): AppErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('CoopMS mobile render error', error, info.componentStack);
  }

  render() {
    if (this.state.error) {
      return (
        <FatalScreen
          title="CoopMS stopped loading"
          message="The mobile app hit a startup error. Restart the Expo session after reviewing the message below."
          details={this.state.error.message}
        />
      );
    }

    return this.props.children;
  }
}

async function tryRegisterPushToken(token: string) {
  try {
    const permission = await Notifications.requestPermissionsAsync();
    if (!permission.granted) return;
    const projectId =
      Constants.easConfig?.projectId ||
      (Constants.expoConfig?.extra?.eas as { projectId?: string } | undefined)?.projectId;

    if (!projectId) return;

    const push = await Notifications.getExpoPushTokenAsync({ projectId });
    await registerDevice(token, push.data, Platform.OS, `${Platform.OS} device`);
  } catch {
    // Push setup should never block login or dashboard access.
  }
}

function TenantScreen({ onReady }: { onReady: () => void }) {
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState(false);

  async function connect() {
    if (!code.trim()) {
      Alert.alert('Cooperative code', 'Enter your cooperative code, e.g. smtcoop.');
      return;
    }
    setBusy(true);
    try {
      const tenant = await resolveTenant(code);
      await setTenant(tenant.base, tenant.coopName);
      onReady();
    } catch (error) {
      Alert.alert('Not found', error instanceof Error ? error.message : 'Check the code and try again.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <SafeAreaView style={styles.authRoot}>
      <StatusBar barStyle="light-content" />
      <KeyboardAvoidingView behavior={Platform.OS === 'ios' ? 'padding' : undefined} style={styles.authContent}>
        <View style={styles.logoMark}>
          <Ionicons name="people-circle" size={42} color={NAVY} />
        </View>
        <Text style={styles.brand}>COOPMS</Text>
        <Text style={styles.tagline}>Digital Cooperative Management Platform</Text>
        <View style={styles.loginCard}>
          <Text style={styles.loginTitle}>Find your cooperative</Text>
          <Text style={styles.helperText}>Enter the code your society gave you, e.g. smtcoop.</Text>
          <TextInput
            value={code}
            onChangeText={setCode}
            autoCapitalize="none"
            autoCorrect={false}
            placeholder="Cooperative code"
            style={styles.input}
          />
          <Pressable style={[styles.primaryButton, busy && styles.disabled]} onPress={connect} disabled={busy}>
            {busy ? <ActivityIndicator color="#fff" /> : <Text style={styles.primaryButtonText}>Continue</Text>}
          </Pressable>
        </View>
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}

function LoginScreen({ onLogin, coopName, onChangeCooperative }: { onLogin: (token: string) => void; coopName: string; onChangeCooperative: () => void }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [otp, setOtp] = useState('');
  const [needOtp, setNeedOtp] = useState(false);
  const [busy, setBusy] = useState(false);
  const [showReset, setShowReset] = useState(false);
  const [resetIdentifier, setResetIdentifier] = useState('');
  const [resetBusy, setResetBusy] = useState(false);

  async function submit() {
    if (!username.trim() || !password) {
      Alert.alert('Missing details', 'Enter your CoopMS username/email and password.');
      return;
    }
    setBusy(true);
    try {
      const response = await login(username.trim(), password, otp.trim());
      onLogin(response.token);
      void tryRegisterPushToken(response.token);
    } catch (error) {
      if (error instanceof ApiError && (error.code === 'otp_required' || error.code === 'otp_invalid')) {
        setNeedOtp(true);
        Alert.alert(error.code === 'otp_invalid' ? 'Code not accepted' : 'Two-factor code needed', error.message);
      } else {
        Alert.alert('Login failed', error instanceof Error ? error.message : 'Please try again.');
      }
    } finally {
      setBusy(false);
    }
  }

  async function sendReset() {
    const identifier = resetIdentifier.trim() || username.trim();
    if (!identifier) {
      Alert.alert('Password reset', 'Enter your email or username first.');
      return;
    }
    setResetBusy(true);
    try {
      const response = await requestPasswordReset(identifier);
      Alert.alert('Password reset', response.message || 'If an account exists, a reset link will be sent.');
      setShowReset(false);
    } catch (error) {
      Alert.alert('Unable to send reset link', error instanceof Error ? error.message : 'Please try again.');
    } finally {
      setResetBusy(false);
    }
  }

  return (
    <SafeAreaView style={styles.authRoot}>
      <StatusBar barStyle="light-content" />
      <KeyboardAvoidingView behavior={Platform.OS === 'ios' ? 'padding' : undefined} style={styles.authContent}>
        <View style={styles.logoMark}>
          <Ionicons name="people-circle" size={42} color={NAVY} />
        </View>
        <Text style={styles.brand}>COOPMS</Text>
        <Text style={styles.tagline}>Digital Cooperative Management Platform</Text>
        <View style={styles.loginCard}>
          <Text style={styles.loginTitle}>Sign in to {coopName || 'your cooperative'}</Text>
          <TextInput
            value={username}
            onChangeText={setUsername}
            autoCapitalize="none"
            keyboardType="email-address"
            placeholder="Email or username"
            style={styles.input}
          />
          <TextInput
            value={password}
            onChangeText={setPassword}
            secureTextEntry
            placeholder="Password"
            style={styles.input}
          />
          {needOtp ? (
            <TextInput
              value={otp}
              onChangeText={setOtp}
              keyboardType="number-pad"
              autoCapitalize="none"
              placeholder="6-digit code (or backup code)"
              style={styles.input}
            />
          ) : null}
          <Pressable style={[styles.primaryButton, busy && styles.disabled]} onPress={submit} disabled={busy}>
            {busy ? <ActivityIndicator color="#fff" /> : <Text style={styles.primaryButtonText}>Sign In</Text>}
          </Pressable>
          <Pressable onPress={() => setShowReset((value) => !value)} style={styles.textButton}>
            <Text style={styles.linkText}>Forgot password?</Text>
          </Pressable>
          {showReset ? (
            <View style={styles.resetPanel}>
              <Text style={styles.helperText}>We will send a secure reset link to your registered email.</Text>
              <TextInput
                value={resetIdentifier}
                onChangeText={setResetIdentifier}
                autoCapitalize="none"
                keyboardType="email-address"
                placeholder="Email or username"
                style={styles.inputLight}
              />
              <Pressable style={[styles.secondaryButton, resetBusy && styles.disabled]} onPress={sendReset} disabled={resetBusy}>
                <Text style={styles.secondaryButtonText}>{resetBusy ? 'Sending...' : 'Send Reset Link'}</Text>
              </Pressable>
            </View>
          ) : null}
          <Pressable onPress={onChangeCooperative}>
            <Text style={styles.apiHint}>{getApiBase().replace('https://', '') || 'your cooperative'} · Change</Text>
          </Pressable>
        </View>
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}

function Header({ title, onLogout }: { title: string; onLogout: () => void }) {
  return (
    <View style={styles.header}>
      <View>
        <Text style={styles.headerEyebrow}>COOPMS</Text>
        <Text style={styles.headerTitle}>{title}</Text>
      </View>
      <Pressable onPress={onLogout} style={styles.iconButton} hitSlop={10}>
        <Ionicons name="log-out-outline" size={20} color={NAVY} />
      </Pressable>
    </View>
  );
}

function StatCard({
  label,
  value,
  tone,
  icon,
  hint,
  onPress
}: {
  label: string;
  value: string;
  tone?: 'yellow' | 'green';
  icon?: keyof typeof Ionicons.glyphMap;
  hint?: string;
  onPress?: () => void;
}) {
  const content = (
    <>
      <View style={styles.statTopLine}>
        <Text style={styles.statLabel}>{label}</Text>
        {icon ? <Ionicons name={icon} size={18} color={tone === 'yellow' ? NAVY : BLUE} /> : null}
      </View>
      <Text style={styles.statValue}>{value}</Text>
      {hint ? <Text style={styles.statHint}>{hint}</Text> : null}
    </>
  );

  if (onPress) {
    return (
      <Pressable
        onPress={onPress}
        style={({ pressed }) => [
          styles.statCard,
          tone === 'yellow' && styles.statYellow,
          tone === 'green' && styles.statGreen,
          pressed && styles.pressed
        ]}
      >
        {content}
      </Pressable>
    );
  }

  return (
    <View
      style={[
        styles.statCard,
        tone === 'yellow' && styles.statYellow,
        tone === 'green' && styles.statGreen
      ]}
    >
      {content}
    </View>
  );
}

function QuickAction({
  icon,
  label,
  helper,
  onPress,
  tone
}: {
  icon: keyof typeof Ionicons.glyphMap;
  label: string;
  helper: string;
  onPress: () => void;
  tone?: 'yellow';
}) {
  return (
    <Pressable style={({ pressed }) => [styles.quickAction, tone === 'yellow' && styles.quickActionYellow, pressed && styles.pressed]} onPress={onPress}>
      <View style={styles.quickIcon}>
        <Ionicons name={icon} size={20} color={tone === 'yellow' ? NAVY : BLUE} />
      </View>
      <View style={styles.quickText}>
        <Text style={styles.quickLabel}>{label}</Text>
        <Text style={styles.quickHelper}>{helper}</Text>
      </View>
      <Ionicons name="chevron-forward" size={18} color="#8A95A6" />
    </Pressable>
  );
}

function HomeScreen({ data, reload, setActive }: { data: DashboardPayload; reload: () => void; setActive: (tab: Tab) => void }) {
  const completion = data.member.profile_completion;
  const latestSavings = data.savings.slice(0, 3);
  const pendingLoan = data.loans.find((loan) => loan.status === 'pending');
  const activeLoan = data.loans.find((loan) => loan.status === 'active');
  return (
    <ScrollView contentContainerStyle={styles.screen}>
      <View style={styles.memberBand}>
        <View>
          <Text style={styles.memberGreeting}>Welcome back</Text>
          <Text style={styles.memberName}>{data.member.full_name}</Text>
          <Text style={styles.memberMeta}>{data.member.member_number} - {data.member.status}</Text>
        </View>
        {completion.certified_member ? (
          <View style={styles.certifiedBadge}>
            <Ionicons name="shield-checkmark" size={16} color={NAVY} />
            <Text style={styles.certifiedText}>Certified</Text>
          </View>
        ) : null}
      </View>

      <View style={styles.statGrid}>
        <StatCard label="Savings" value={money(data.member.total_savings)} icon="wallet" hint="View statement" onPress={() => setActive('savings')} />
        <StatCard label="Share Capital" value={money(data.member.share_capital)} icon="layers" tone="yellow" hint="Member equity" onPress={() => setActive('savings')} />
        <StatCard label="Loan Balance" value={money(data.summary.active_loan_balance)} icon="cash" hint="Open loan book" onPress={() => setActive('loans')} />
        <StatCard label="Profile" value={`${completion.percent}%`} icon="person-circle" tone={completion.percent === 100 ? 'green' : 'yellow'} hint={completion.certified_member ? 'Certified member' : 'Complete profile'} onPress={() => setActive('profile')} />
      </View>

      <View style={styles.card}>
        <Text style={styles.cardTitle}>Loan Application Limits</Text>
        {Object.entries(data.member.loan_type_limits || {}).map(([name, limit]) => (
          <QuickAction key={name} icon="calculator" label={name}
            helper={`Up to ${money(limit.eligible_amount)} · ${limit.max_tenure_months} months`}
            onPress={() => setActive('loans')} />
        ))}
        <Text>Subject to eligibility checks and committee approval.</Text>
      </View>

      <View style={styles.card}>
        <Text style={styles.cardTitle}>Quick Actions</Text>
        <QuickAction icon="person-circle" label={completion.percent === 100 ? 'View Profile' : 'Complete Profile'} helper={completion.percent === 100 ? 'Review your member information' : 'Finish setup to become transaction-ready'} onPress={() => setActive('profile')} tone={completion.percent < 100 ? 'yellow' : undefined} />
        <QuickAction icon="document-text" label="Savings Statement" helper="See contributions from inception" onPress={() => setActive('savings')} />
        <QuickAction icon="calculator" label="Loan Calculator" helper={pendingLoan ? 'Review pending application' : 'Preview repayment before applying'} onPress={() => setActive('loans')} />
        <QuickAction icon="notifications" label="Notifications" helper={`${data.summary.unread_notifications} unread message${data.summary.unread_notifications === 1 ? '' : 's'}`} onPress={() => setActive('notifications')} />
      </View>

      <View style={styles.card}>
        <View style={styles.rowBetween}>
          <Text style={styles.cardTitle}>Next Best Action</Text>
          <Pressable onPress={reload} hitSlop={10}>
            <Ionicons name="refresh" size={18} color={BLUE} />
          </Pressable>
        </View>
        {completion.percent < 100 ? (
          <Pressable style={({ pressed }) => [styles.actionNotice, pressed && styles.pressed]} onPress={() => setActive('profile')}>
            <Text style={styles.rowTitle}>Complete your profile</Text>
            <Text style={styles.rowSub}>Your profile is {completion.percent}% complete. Finish the remaining fields to improve readiness to transact.</Text>
          </Pressable>
        ) : pendingLoan ? (
          <Pressable style={({ pressed }) => [styles.actionNotice, pressed && styles.pressed]} onPress={() => setActive('loans')}>
            <Text style={styles.rowTitle}>Loan application pending</Text>
            <Text style={styles.rowSub}>{pendingLoan.loan_number} is still in workflow. Open loans to review or withdraw before approval.</Text>
          </Pressable>
        ) : activeLoan ? (
          <Pressable style={({ pressed }) => [styles.actionNotice, pressed && styles.pressed]} onPress={() => setActive('loans')}>
            <Text style={styles.rowTitle}>Track loan balance</Text>
            <Text style={styles.rowSub}>{activeLoan.loan_number} has a balance of {money(activeLoan.balance)}.</Text>
          </Pressable>
        ) : (
          <Pressable style={({ pressed }) => [styles.actionNotice, pressed && styles.pressed]} onPress={() => setActive('loans')}>
            <Text style={styles.rowTitle}>Estimate a loan</Text>
            <Text style={styles.rowSub}>Preview repayment and affordability before submitting a request.</Text>
          </Pressable>
        )}
      </View>

      <View style={styles.card}>
        <View style={styles.rowBetween}>
          <Text style={styles.cardTitle}>Recent Savings</Text>
          <Pressable onPress={() => setActive('savings')}>
            <Text style={styles.linkText}>View all</Text>
          </Pressable>
        </View>
        {latestSavings.map((row) => (
          <Pressable key={row.id} style={({ pressed }) => [styles.listRow, pressed && styles.pressed]} onPress={() => setActive('savings')}>
            <View style={styles.rowBetween}>
              <Text style={styles.rowTitle}>{money(row.amount)}</Text>
              <Text style={styles.dateText}>{row.date?.slice(0, 10) || row.month}</Text>
            </View>
            <Text style={styles.rowSub}>{row.month} - {row.receipt_number || 'No receipt'}</Text>
          </Pressable>
        ))}
        {latestSavings.length === 0 ? <Text style={styles.emptyText}>No savings records found yet.</Text> : null}
      </View>

      <View style={styles.card}>
        <View style={styles.rowBetween}>
          <Text style={styles.cardTitle}>Recent Notifications</Text>
          <Pressable onPress={() => setActive('notifications')}>
            <Text style={styles.linkText}>Open</Text>
          </Pressable>
        </View>
        {data.notifications.slice(0, 2).map((item) => (
          <Pressable key={item.id} style={({ pressed }) => [styles.listRow, pressed && styles.pressed]} onPress={() => setActive('notifications')}>
            <Text style={styles.rowTitle}>{item.title}</Text>
            <Text style={styles.rowSub}>{item.message}</Text>
          </Pressable>
        ))}
        {data.notifications.length === 0 ? <Text style={styles.emptyText}>No notifications yet.</Text> : null}
      </View>
    </ScrollView>
  );
}

function ProfileScreen({ token, data, reload }: { token: string; data: DashboardPayload; reload: () => void }) {
  const [profile, setProfile] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [currentPassword, setCurrentPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [passwordBusy, setPasswordBusy] = useState(false);

  useEffect(() => {
    getProfile(token).then((response) => setProfile(response.profile)).catch(() => undefined);
  }, [token]);

  async function save() {
    setBusy(true);
    try {
      await updateProfile(token, profile);
      reload();
      Alert.alert('Profile saved', 'Your member profile has been updated.');
    } catch (error) {
      Alert.alert('Unable to save', error instanceof Error ? error.message : 'Please try again.');
    } finally {
      setBusy(false);
    }
  }

  async function savePassword() {
    if (!currentPassword || !newPassword || !confirmPassword) {
      Alert.alert('Change password', 'Enter your current password and confirm the new password.');
      return;
    }
    setPasswordBusy(true);
    try {
      const response = await changePassword(token, currentPassword, newPassword, confirmPassword);
      setCurrentPassword('');
      setNewPassword('');
      setConfirmPassword('');
      Alert.alert('Password changed', response.message || 'Your password has been updated.');
    } catch (error) {
      Alert.alert('Unable to change password', error instanceof Error ? error.message : 'Please try again.');
    } finally {
      setPasswordBusy(false);
    }
  }

  const completion = data.member.profile_completion;
  const editable = ['phone', 'address', 'city', 'state', 'country', 'occupation', 'date_of_birth', 'emergency_contact_name', 'emergency_contact_phone', 'next_of_kin'];

  return (
    <ScrollView contentContainerStyle={styles.screen}>
      <View style={styles.card}>
        <Text style={styles.cardTitle}>Profile Readiness</Text>
        <View style={styles.progressShell}>
          <View style={[styles.progressFill, { width: `${completion.percent}%` }]} />
        </View>
        <Text style={styles.rowSub}>{completion.percent}% complete</Text>
      </View>

      <View style={styles.card}>
        <Text style={styles.cardTitle}>Update Details</Text>
        {editable.map((field) => (
          <View key={field} style={styles.fieldBlock}>
            <Text style={styles.fieldLabel}>{field.replace(/_/g, ' ')}</Text>
            <TextInput
              value={profile[field] || ''}
              onChangeText={(value) => setProfile((current) => ({ ...current, [field]: value }))}
              style={styles.inputLight}
            />
          </View>
        ))}
        <Pressable style={[styles.primaryButton, busy && styles.disabled]} onPress={save} disabled={busy}>
          <Text style={styles.primaryButtonText}>{busy ? 'Saving...' : 'Save Profile'}</Text>
        </Pressable>
      </View>

      <View style={styles.card}>
        <Text style={styles.cardTitle}>Security</Text>
        <Text style={styles.rowSub}>Change your password using the cooperative's current password policy.</Text>
        <View style={styles.fieldBlock}>
          <Text style={styles.fieldLabel}>Current password</Text>
          <TextInput value={currentPassword} onChangeText={setCurrentPassword} secureTextEntry style={styles.inputLight} />
        </View>
        <View style={styles.fieldBlock}>
          <Text style={styles.fieldLabel}>New password</Text>
          <TextInput value={newPassword} onChangeText={setNewPassword} secureTextEntry style={styles.inputLight} />
        </View>
        <View style={styles.fieldBlock}>
          <Text style={styles.fieldLabel}>Confirm new password</Text>
          <TextInput value={confirmPassword} onChangeText={setConfirmPassword} secureTextEntry style={styles.inputLight} />
        </View>
        <Pressable style={[styles.secondaryButton, passwordBusy && styles.disabled]} onPress={savePassword} disabled={passwordBusy}>
          <Text style={styles.secondaryButtonText}>{passwordBusy ? 'Saving...' : 'Change Password'}</Text>
        </Pressable>
      </View>

      <Pressable
        accessibilityRole="link"
        style={({ pressed }) => [styles.privacyLink, pressed && styles.pressed]}
        onPress={() => Linking.openURL(PRIVACY_URL)}
      >
        <Ionicons name="shield-checkmark-outline" size={15} color={MUTED} />
        <Text style={styles.privacyLinkText}>Privacy Policy</Text>
      </Pressable>

      <Text style={styles.buildStamp}>{buildStamp()}</Text>
    </ScrollView>
  );
}

/** The member's own account number, plus what they want transfers to pay for.
 *  Renders nothing when the cooperative does not issue account numbers, so the
 *  savings screen is unchanged for societies that have not switched it on. */
function PayInCard({ token }: { token: string }) {
  const [data, setData] = useState<PayInPayload | null>(null);
  const [saving, setSaving] = useState(false);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    getPayIn(token).then(setData).catch(() => undefined);
  }, [token]);

  if (!data || !data.enabled || !data.account) return null;
  const account = data.account;

  const copy = async () => {
    await Clipboard.setStringAsync(account.account_number);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const choose = async (key: string) => {
    if (saving || key === data.preference) return;
    setSaving(true);
    try {
      setData(await setPayInPreference(token, key));
    } catch (error) {
      Alert.alert('Could not save', error instanceof Error ? error.message : 'Please try again.');
    } finally {
      setSaving(false);
    }
  };

  return (
    <View style={styles.card}>
      <Text style={styles.cardTitle}>Your Account Number</Text>
      <Text style={styles.quickHelper}>
        Transfer to this account from any bank app and it is credited to you automatically.
        You do not need to send proof or quote a reference.
      </Text>

      <View style={styles.payInRow}>
        <View style={{ flex: 1 }}>
          <Text style={styles.payInNumber} selectable>{account.account_number}</Text>
          <Text style={styles.rowSub}>{account.bank_name} - {account.account_name}</Text>
        </View>
        <Pressable
          accessibilityRole="button"
          accessibilityLabel="Copy account number"
          style={({ pressed }) => [styles.copyButton, pressed && styles.pressed]}
          onPress={copy}
        >
          <Ionicons name={copied ? 'checkmark' : 'copy-outline'} size={16} color={BLUE} />
          <Text style={styles.copyButtonText}>{copied ? 'Copied' : 'Copy'}</Text>
        </Pressable>
      </View>

      {data.choices.length > 0 ? (
        <View style={styles.fieldBlock}>
          <Text style={styles.fieldLabel}>What should your transfers pay for?</Text>
          <View style={styles.optionWrap}>
            {data.choices.map((choice) => (
              <OptionChip
                key={choice.key || 'coop-default'}
                label={choice.label}
                selected={data.preference === choice.key}
                onPress={() => choose(choice.key)}
              />
            ))}
          </View>
          <Text style={styles.quickHelper}>Anything left over after that goes into your savings.</Text>
        </View>
      ) : null}
    </View>
  );
}

function SavingsScreen({ token }: { token: string }) {
  const [rows, setRows] = useState<SavingRow[]>([]);
  const [balance, setBalance] = useState(0);

  useEffect(() => {
    getSavings(token).then((response) => {
      setRows(response.rows);
      setBalance(response.balance);
    }).catch(() => undefined);
  }, [token]);

  return (
    <ScrollView contentContainerStyle={styles.screen}>
      <StatCard label="Savings Balance" value={money(balance)} />
      <PayInCard token={token} />
      <View style={styles.card}>
        <Text style={styles.cardTitle}>Savings Statement</Text>
        {rows.map((row) => (
          <View key={row.id} style={styles.listRow}>
            <Text style={styles.rowTitle}>{money(row.amount)}</Text>
            <Text style={styles.rowSub}>{row.month} - {row.receipt_number || 'No receipt'} - {row.date?.slice(0, 10) || ''}</Text>
          </View>
        ))}
        {rows.length === 0 ? <Text style={styles.emptyText}>No savings records found.</Text> : null}
      </View>
    </ScrollView>
  );
}

function OptionChip({ label, selected, onPress }: { label: string; selected: boolean; onPress: () => void }) {
  return (
    <Pressable style={({ pressed }) => [styles.optionChip, selected && styles.optionChipSelected, pressed && styles.pressed]} onPress={onPress}>
      <Text style={[styles.optionChipText, selected && styles.optionChipTextSelected]}>{label}</Text>
    </Pressable>
  );
}

function GuarantorChoice({
  guarantor,
  selected,
  onToggle
}: {
  guarantor: GuarantorOption;
  selected: boolean;
  onToggle: () => void;
}) {
  return (
    <Pressable style={({ pressed }) => [styles.guarantorChoice, selected && styles.guarantorSelected, pressed && styles.pressed]} onPress={onToggle}>
      <View style={[styles.guarantorCheck, selected && styles.guarantorCheckSelected]}>
        {selected ? <Ionicons name="checkmark" size={16} color="#fff" /> : null}
      </View>
      <View style={styles.quickText}>
        <Text style={styles.quickLabel}>{guarantor.full_name}</Text>
        <Text style={styles.quickHelper}>{guarantor.member_number} - {guarantor.phone || guarantor.email || 'Active member'}</Text>
      </View>
    </Pressable>
  );
}

function LoansScreen({ token, dashboard, reload }: { token: string; dashboard: DashboardPayload; reload: () => void }) {
  const [selected, setSelected] = useState<Loan | null>(null);
  const [reason, setReason] = useState('');
  const [showApply, setShowApply] = useState(false);
  const [loanOptions, setLoanOptions] = useState<LoanOptionsPayload | null>(null);
  const [amount, setAmount] = useState('');
  const [purpose, setPurpose] = useState('Regular');
  const [tenure, setTenure] = useState('6');
  const [signature, setSignature] = useState('');
  const [collateral, setCollateral] = useState('standing_order');
  const [guarantors, setGuarantors] = useState<number[]>([]);
  const [guarantorQuery, setGuarantorQuery] = useState('');
  const [preview, setPreview] = useState<{ monthly_payment: number; total_repayment: number; total_interest: number } | null>(null);
  const [busyApply, setBusyApply] = useState(false);
  const selectedGuarantors = useMemo(() => {
    const choices = loanOptions?.eligible_guarantors || [];
    return choices.filter((item) => guarantors.includes(item.id));
  }, [loanOptions, guarantors]);
  const filteredGuarantors = useMemo(() => {
    const choices = loanOptions?.eligible_guarantors || [];
    const query = guarantorQuery.trim().toLowerCase();
    if (!query) return choices.slice(0, 25);
    return choices.filter((item) => (
      item.full_name.toLowerCase().includes(query)
      || item.member_number.toLowerCase().includes(query)
      || (item.email || '').toLowerCase().includes(query)
      || (item.phone || '').toLowerCase().includes(query)
    )).slice(0, 30);
  }, [loanOptions, guarantorQuery]);

  useEffect(() => {
    getLoanOptions(token).then((response) => {
      setLoanOptions(response);
      if (response.purposes.length > 0 && !response.purposes.some((item) => item.value === purpose)) {
        setPurpose(response.purposes[0].value);
      }
      if (response.collateral_options.length > 0 && !response.collateral_options.some((item) => item.value === collateral)) {
        setCollateral(response.collateral_options[0].value);
      }
    }).catch(() => undefined);
  }, [token]);

  function toggleGuarantor(id: number) {
    setGuarantors((current) => (
      current.includes(id) ? current.filter((item) => item !== id) : [...current, id]
    ));
  }

  async function openLoan(loan: Loan) {
    const response = await getLoanDetail(token, loan.id);
    setSelected(response.loan);
  }

  async function withdraw() {
    if (!selected) return;
    try {
      const response = await withdrawLoan(token, selected.id, reason);
      setSelected(response.loan);
      setReason('');
      await reload();
      Alert.alert('Application withdrawn', 'The loan application has left the approval workflow.');
    } catch (error) {
      Alert.alert('Unable to withdraw', error instanceof Error ? error.message : 'Please try again.');
    }
  }

  async function runPreview() {
    try {
      const response = await previewLoanSchedule(token, {
        amount: Number(amount),
        purpose,
        tenure: Number(tenure)
      });
      setPreview({
        monthly_payment: response.monthly_payment,
        total_repayment: response.total_repayment,
        total_interest: response.total_interest
      });
    } catch (error) {
      Alert.alert('Unable to preview', error instanceof Error ? error.message : 'Check the amount and tenure.');
    }
  }

  async function submitLoan() {
    if (!preview) {
      Alert.alert('Preview required', 'Preview and accept the repayment schedule before submitting.');
      return;
    }
    setBusyApply(true);
    try {
      await applyForLoan(token, {
        amount: Number(amount),
        purpose,
        tenure: Number(tenure),
        payment_collateral_type: collateral,
        guarantor_ids: guarantors,
        signature_name: signature,
        accept_terms: true,
        data_processing_consent: true,
        repayment_schedule_accepted: true,
        hr_affordability_consent: true,
        credit_check_consent: true,
        bank_statement_ack: true
      });
      setShowApply(false);
      setPreview(null);
      setAmount('');
      setSignature('');
      setGuarantors([]);
      setGuarantorQuery('');
      reload();
      Alert.alert('Application submitted', 'Your loan request has entered the approval workflow.');
    } catch (error) {
      Alert.alert('Unable to submit', error instanceof Error ? error.message : 'Please try again.');
    } finally {
      setBusyApply(false);
    }
  }

  return (
    <ScrollView contentContainerStyle={styles.screen}>
      <Pressable style={styles.primaryButton} onPress={() => setShowApply((value) => !value)}>
        <Text style={styles.primaryButtonText}>{showApply ? 'Close Application Form' : 'Apply for Loan'}</Text>
      </Pressable>

      {showApply ? (
        <View style={styles.card}>
          <Text style={styles.cardTitle}>New Loan Application</Text>
          <Text style={styles.rowSub}>Enter the loan details, preview the schedule, then submit when you accept the repayment terms.</Text>
          <View style={styles.fieldBlock}>
            <Text style={styles.fieldLabel}>Amount</Text>
            <TextInput value={amount} onChangeText={setAmount} keyboardType="numeric" style={styles.inputLight} placeholder="50000" />
          </View>
          <View style={styles.fieldBlock}>
            <Text style={styles.fieldLabel}>Purpose</Text>
            <View style={styles.optionWrap}>
              {(loanOptions?.purposes || [{ value: 'Regular', label: 'Regular' }]).map((option) => (
                <OptionChip
                  key={option.value}
                  label={option.label}
                  selected={purpose === option.value}
                  onPress={() => {
                    setPurpose(option.value);
                    setPreview(null);
                  }}
                />
              ))}
            </View>
          </View>
          <View style={styles.fieldBlock}>
            <Text style={styles.fieldLabel}>Tenure months</Text>
            <TextInput value={tenure} onChangeText={setTenure} keyboardType="numeric" style={styles.inputLight} placeholder="6" />
          </View>
          <View style={styles.fieldBlock}>
            <Text style={styles.fieldLabel}>Collateral</Text>
            <View style={styles.optionStack}>
              {(loanOptions?.collateral_options || [{ value: 'standing_order', label: 'Standing order / salary deduction', description: 'Repayment is deducted automatically.' }]).map((option) => (
                <Pressable
                  key={option.value}
                  style={({ pressed }) => [styles.collateralOption, collateral === option.value && styles.collateralSelected, pressed && styles.pressed]}
                  onPress={() => setCollateral(option.value)}
                >
                  <View style={styles.rowBetween}>
                    <Text style={styles.rowTitle}>{option.label}</Text>
                    {collateral === option.value ? <Ionicons name="checkmark-circle" size={20} color={BLUE} /> : null}
                  </View>
                  <Text style={styles.rowSub}>{option.description}</Text>
                </Pressable>
              ))}
            </View>
          </View>
          <View style={styles.fieldBlock}>
            <View style={styles.rowBetween}>
              <Text style={styles.fieldLabel}>Guarantors</Text>
              <Text style={styles.selectionCount}>{guarantors.length} selected</Text>
            </View>
            <Text style={styles.rowSub}>
              Search and select {loanOptions?.guarantors_required ?? 0} active member{(loanOptions?.guarantors_required ?? 0) === 1 ? '' : 's'}.
            </Text>
            {selectedGuarantors.length > 0 ? (
              <View style={styles.selectedWrap}>
                {selectedGuarantors.map((guarantor) => (
                  <Pressable key={guarantor.id} style={styles.selectedPill} onPress={() => toggleGuarantor(guarantor.id)}>
                    <Text style={styles.selectedPillText}>{guarantor.full_name}</Text>
                    <Ionicons name="close" size={14} color={NAVY} />
                  </Pressable>
                ))}
              </View>
            ) : null}
            <View style={styles.searchBox}>
              <Ionicons name="search" size={18} color="#7D8797" />
              <TextInput
                value={guarantorQuery}
                onChangeText={setGuarantorQuery}
                autoCapitalize="none"
                autoCorrect={false}
                placeholder="Search member name, number, phone, or email"
                style={styles.searchInput}
              />
              {guarantorQuery ? (
                <Pressable onPress={() => setGuarantorQuery('')} hitSlop={8}>
                  <Ionicons name="close-circle" size={18} color="#7D8797" />
                </Pressable>
              ) : null}
            </View>
            <View style={styles.searchResults}>
              {filteredGuarantors.map((guarantor) => (
                <GuarantorChoice
                  key={guarantor.id}
                  guarantor={guarantor}
                  selected={guarantors.includes(guarantor.id)}
                  onToggle={() => toggleGuarantor(guarantor.id)}
                />
              ))}
            </View>
            {loanOptions && loanOptions.eligible_guarantors.length === 0 ? (
              <Text style={styles.emptyText}>No eligible active guarantors found. Contact the cooperative office.</Text>
            ) : null}
            {loanOptions && loanOptions.eligible_guarantors.length > 0 && filteredGuarantors.length === 0 ? (
              <Text style={styles.emptyText}>No member matched your search.</Text>
            ) : null}
            {loanOptions && !guarantorQuery && loanOptions.eligible_guarantors.length > filteredGuarantors.length ? (
              <Text style={styles.rowSub}>Showing first {filteredGuarantors.length}. Use search to find other members.</Text>
            ) : null}
          </View>
          <Pressable style={styles.secondaryButton} onPress={runPreview}>
            <Text style={styles.secondaryButtonText}>Preview Schedule</Text>
          </Pressable>
          {preview ? (
            <View style={styles.previewBox}>
              <Text style={styles.detailLine}>Monthly payment: {money(preview.monthly_payment)}</Text>
              <Text style={styles.detailLine}>Total repayment: {money(preview.total_repayment)}</Text>
              <Text style={styles.detailLine}>Total interest: {money(preview.total_interest)}</Text>
            </View>
          ) : null}
          <View style={styles.fieldBlock}>
            <Text style={styles.fieldLabel}>Signature name</Text>
            <TextInput value={signature} onChangeText={setSignature} style={styles.inputLight} placeholder={dashboard.member.full_name} />
          </View>
          <Text style={styles.disclaimer}>
            By submitting, you accept the repayment schedule and permit the cooperative to process required application, affordability, and collateral checks.
          </Text>
          <Pressable style={[styles.primaryButton, busyApply && styles.disabled]} onPress={submitLoan} disabled={busyApply}>
            <Text style={styles.primaryButtonText}>{busyApply ? 'Submitting...' : 'Submit Application'}</Text>
          </Pressable>
        </View>
      ) : null}

      <View style={styles.card}>
        <Text style={styles.cardTitle}>Loans</Text>
        {dashboard.loans.map((loan) => (
          <Pressable key={loan.id} style={styles.listRow} onPress={() => openLoan(loan)}>
            <View style={styles.rowBetween}>
              <Text style={styles.rowTitle}>{loan.loan_number}</Text>
              <Text style={[styles.statusPill, loan.status === 'active' && styles.statusActive]}>{loan.status}</Text>
            </View>
            <Text style={styles.rowSub}>{loan.purpose} - {money(loan.amount)} - {loan.tenure} months</Text>
          </Pressable>
        ))}
        {dashboard.loans.length === 0 ? <Text style={styles.emptyText}>No loan applications yet.</Text> : null}
      </View>

      {selected ? (
        <View style={styles.card}>
          <Text style={styles.cardTitle}>{selected.loan_number}</Text>
          <Text style={styles.detailLine}>Amount: {money(selected.amount)}</Text>
          <Text style={styles.detailLine}>Total repayment: {money(selected.total_repayment)}</Text>
          <Text style={styles.detailLine}>Balance: {selected.is_disbursed ? money(selected.balance) : 'Not disbursed'}</Text>
          <Text style={styles.detailLine}>Status: {selected.status}</Text>
          {selected.status === 'pending' ? (
            <>
              <TextInput
                value={reason}
                onChangeText={setReason}
                placeholder="Reason for withdrawal"
                style={styles.inputLight}
              />
              <Pressable style={styles.dangerButton} onPress={withdraw}>
                <Text style={styles.dangerButtonText}>Withdraw Application</Text>
              </Pressable>
            </>
          ) : null}
        </View>
      ) : null}
    </ScrollView>
  );
}

function NotificationsScreen({ token, initial }: { token: string; initial: MobileNotification[] }) {
  const [items, setItems] = useState(initial);

  async function refresh() {
    const response = await getNotifications(token);
    setItems(response.notifications);
  }

  async function markAll() {
    await markAllNotificationsRead(token);
    await refresh();
  }

  return (
    <ScrollView contentContainerStyle={styles.screen}>
      <View style={styles.card}>
        <View style={styles.rowBetween}>
          <Text style={styles.cardTitle}>Notifications</Text>
          <Pressable onPress={markAll}>
            <Text style={styles.linkText}>Mark all read</Text>
          </Pressable>
        </View>
        {items.map((item) => (
          <View key={item.id} style={styles.listRow}>
            <Text style={styles.rowTitle}>{item.title}</Text>
            <Text style={styles.rowSub}>{item.message}</Text>
          </View>
        ))}
        {items.length === 0 ? <Text style={styles.emptyText}>No notifications yet.</Text> : null}
      </View>
    </ScrollView>
  );
}

function CtasScreen({ token }: { token: string }) {
  const [data, setData] = useState<CtasPayload | null>(null);
  const [cycleId, setCycleId] = useState<number | null>(null);
  const [target, setTarget] = useState('');
  const [tenure, setTenure] = useState('');
  const [signature, setSignature] = useState('');
  const [terms, setTerms] = useState(false);
  const [busy, setBusy] = useState(false);

  const load = () => getCtas(token).then(setData).catch(() => undefined);
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [token]);

  if (!data) {
    return <ScrollView contentContainerStyle={styles.screen}><Text style={styles.emptyText}>Loading...</Text></ScrollView>;
  }
  if (!data.enabled) {
    return (
      <ScrollView contentContainerStyle={styles.screen}>
        <View style={styles.card}><Text style={styles.cardTitle}>Target Advance</Text>
          <Text style={styles.emptyText}>This feature is not enabled for your cooperative.</Text></View>
      </ScrollView>
    );
  }

  const submit = async () => {
    if (!cycleId) { Alert.alert('Select a cycle first'); return; }
    if (!terms) { Alert.alert('Please accept the scheme terms'); return; }
    if (!signature.trim()) { Alert.alert('Type your full name as your signature'); return; }
    setBusy(true);
    try {
      await applyCtas(token, {
        cycle_id: cycleId, target_amount: Number(target || 0), tenure_months: Number(tenure || 0),
        terms_accepted: terms, signature_name: signature.trim(),
      });
      Alert.alert('Submitted', 'Your target-advance application has been submitted for review.');
      setTarget(''); setTenure(''); setSignature(''); setTerms(false); setCycleId(null);
      load();
    } catch (e) {
      Alert.alert('Could not apply', e instanceof Error ? e.message : 'Please try again.');
    } finally { setBusy(false); }
  };

  return (
    <ScrollView contentContainerStyle={styles.screen}>
      {data.open_cycles.length > 0 && !data.has_active ? (
        <View style={styles.card}>
          <Text style={styles.cardTitle}>Apply for a Target Advance</Text>
          <Text style={styles.rowSub}>Savings balance: {money(data.savings_balance)}</Text>
          <View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 8, marginVertical: 10 }}>
            {data.open_cycles.map((c) => (
              <OptionChip key={c.id} label={c.name} selected={cycleId === c.id} onPress={() => setCycleId(c.id)} />
            ))}
          </View>
          <View style={styles.fieldBlock}><Text style={styles.fieldLabel}>Target amount</Text>
            <TextInput value={target} onChangeText={setTarget} keyboardType="numeric" style={styles.inputLight} placeholder="200000" /></View>
          <View style={styles.fieldBlock}><Text style={styles.fieldLabel}>Tenure (months)</Text>
            <TextInput value={tenure} onChangeText={setTenure} keyboardType="numeric" style={styles.inputLight} placeholder="6" /></View>
          <View style={styles.fieldBlock}><Text style={styles.fieldLabel}>Signature (your full name)</Text>
            <TextInput value={signature} onChangeText={setSignature} style={styles.inputLight} /></View>
          <Pressable style={{ flexDirection: 'row', alignItems: 'center', gap: 10, marginVertical: 8 }} onPress={() => setTerms((v) => !v)}>
            <View style={[styles.guarantorCheck, terms && styles.guarantorCheckSelected]}>
              {terms ? <Ionicons name="checkmark" size={16} color="#fff" /> : null}</View>
            <Text style={[styles.quickHelper, { flex: 1 }]}>I accept the scheme terms and authorise recovery by monthly deductions.</Text>
          </Pressable>
          <Pressable style={[styles.primaryButton, busy && styles.disabled]} onPress={submit} disabled={busy}>
            <Text style={styles.primaryButtonText}>{busy ? 'Submitting...' : 'Submit application'}</Text></Pressable>
        </View>
      ) : data.has_active ? (
        <View style={styles.card}><Text style={styles.emptyText}>You have an active target advance. You can apply again once it is completed.</Text></View>
      ) : (
        <View style={styles.card}><Text style={styles.emptyText}>No cycles are open for applications right now.</Text></View>
      )}

      <View style={styles.card}>
        <Text style={styles.cardTitle}>My Target Advances</Text>
        {data.subscriptions.map((s) => (
          <View key={s.id} style={styles.listRow}>
            <Text style={styles.rowTitle}>{s.cycle_name} - {money(s.target_amount)}</Text>
            <Text style={styles.rowSub}>Status: {s.status.replace(/_/g, ' ')}{s.payout_month ? ` - payout month ${s.payout_month}` : ''}</Text>
            {s.status === 'active_recovery' || s.status === 'completed' ? (
              <Text style={styles.rowSub}>Recovered {money(s.total_recovered)} of {money(s.target_amount)} ({s.progress}%)</Text>
            ) : null}
            {s.arrears_amount > 0 ? <Text style={[styles.rowSub, { color: '#c0392b' }]}>Arrears: {money(s.arrears_amount)}</Text> : null}
          </View>
        ))}
        {data.subscriptions.length === 0 ? <Text style={styles.emptyText}>No applications yet.</Text> : null}
      </View>
    </ScrollView>
  );
}

function TabBar({ active, setActive, unread, showCtas }: { active: Tab; setActive: (tab: Tab) => void; unread: number; showCtas: boolean }) {
  const tabs: Array<{ key: Tab; icon: keyof typeof Ionicons.glyphMap; label: string }> = [
    { key: 'home', icon: 'home', label: 'Home' },
    { key: 'profile', icon: 'person', label: 'Profile' },
    { key: 'savings', icon: 'wallet', label: 'Savings' },
    { key: 'loans', icon: 'cash', label: 'Loans' },
    ...(showCtas ? [{ key: 'ctas' as Tab, icon: 'sync' as keyof typeof Ionicons.glyphMap, label: 'Advance' }] : []),
    { key: 'notifications', icon: 'notifications', label: 'Alerts' }
  ];
  return (
    <View style={styles.tabBar}>
      {tabs.map((tab) => (
        <Pressable key={tab.key} style={styles.tabButton} onPress={() => setActive(tab.key)} hitSlop={8}>
          <Ionicons name={tab.icon} size={21} color={active === tab.key ? NAVY : '#7D8797'} />
          <Text style={[styles.tabLabel, active === tab.key && styles.tabActive]}>{tab.label}</Text>
          {tab.key === 'notifications' && unread > 0 ? <View style={styles.dot} /> : null}
        </Pressable>
      ))}
    </View>
  );
}

function AppContent() {
  const [token, setToken] = useState<string | null>(null);
  const [dashboard, setDashboard] = useState<DashboardPayload | null>(null);
  const [activeTab, setActiveTab] = useState<Tab>('home');
  const [ctasEnabled, setCtasEnabled] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadingMessage, setLoadingMessage] = useState('Checking your cooperative and secure session...');
  const [hasTenant, setHasTenant] = useState(false);
  const [coopName, setCoopName] = useState('');
  const [startupError, setStartupError] = useState('');
  const [dashboardError, setDashboardError] = useState('');
  const [bootAttempt, setBootAttempt] = useState(0);

  async function reload(currentToken = token) {
    if (!currentToken) return;
    const data = await getDashboard(currentToken);
    setDashboard(data);
  }

  // Whether the optional Target Advance (CTAS) module is on for this cooperative.
  useEffect(() => {
    if (!token) { setCtasEnabled(false); return; }
    getCtas(token).then((r) => setCtasEnabled(!!r.enabled)).catch(() => setCtasEnabled(false));
  }, [token]);

  useEffect(() => {
    let alive = true;
    let settled = false;

    // The watchdog exists for a boot that never comes back — a request left
    // hanging by a dead connection. It MUST be cancelled the moment boot
    // finishes: left armed, it fires over a perfectly good session and throws
    // the member out for no reason.
    const watchdog = setTimeout(() => {
      if (!alive || settled) return;
      setStartupError('Could not reach your cooperative. This is almost always a slow or dropped connection.');
      setLoading(false);
    }, 25000);

    (async () => {
      try {
        setStartupError('');
        setDashboardError('');
        setLoading(true);
        setLoadingMessage('Loading saved cooperative...');
        const ready = await loadTenant();
        if (!alive) return;
        setHasTenant(ready);
        setCoopName(getCoopName());
        if (ready) {
          setLoadingMessage('Checking saved sign-in...');
          const stored = await loadToken();
          if (!alive) return;
          if (stored) {
            setToken(stored);
            try {
              setLoadingMessage('Loading your dashboard...');
              await reload(stored);
              void tryRegisterPushToken(stored);
            } catch (error) {
              if (!alive) return;
              if (error instanceof ApiError && (error.status === 401 || error.status === 403)) {
                await clearToken();
                setToken(null);
              } else {
                setDashboardError(error instanceof Error ? error.message : 'Could not load your dashboard.');
              }
            }
          }
        }
      } catch (error) {
        if (!alive) return;
        // Do NOT wipe the saved cooperative or sign-in here. A failure at this
        // point is nearly always the network, and making the member find their
        // society code again every time the signal drops is what made the app
        // feel broken. If the stored session really is unreadable, the member
        // can choose Start Over on the error screen.
        setStartupError(error instanceof Error ? error.message : 'Could not start CoopMS.');
      } finally {
        settled = true;
        clearTimeout(watchdog);
        if (alive) setLoading(false);
      }
    })();

    return () => {
      alive = false;
      clearTimeout(watchdog);
    };
  }, [bootAttempt]);

  async function onLogin(nextToken: string) {
    try {
      await reload(nextToken);
      setToken(nextToken);
    } catch (error) {
      await clearToken();
      setToken(null);
      const message = error instanceof Error ? error.message : 'Could not load your member dashboard.';
      Alert.alert(
        'Member profile required',
        message.includes('member profile')
          ? 'This mobile app is for member self-service. This account is not linked to a member profile yet. Use the web admin portal, or link this user email to a member record.'
          : message
      );
    }
  }

  async function logout() {
    await clearToken();
    setToken(null);
    setDashboard(null);
    setActiveTab('home');
  }

  async function changeCooperative() {
    await clearTenant();
    await clearToken();
    setToken(null);
    setDashboard(null);
    setHasTenant(false);
    setCoopName('');
    setActiveTab('home');
  }

  const title = useMemo(() => {
    if (activeTab === 'home') return 'Dashboard';
    if (activeTab === 'profile') return 'Profile';
    if (activeTab === 'savings') return 'Savings';
    if (activeTab === 'loans') return 'Loans';
    if (activeTab === 'ctas') return 'Target Advance';
    return 'Notifications';
  }, [activeTab]);

  if (loading) {
    return (
      <SafeAreaView style={styles.loadingRoot}>
        <ActivityIndicator size="large" color={NAVY} />
        <Text style={styles.loadingTitle}>Loading CoopMS</Text>
        <Text style={styles.loadingHint}>{loadingMessage}</Text>
      </SafeAreaView>
    );
  }

  if (startupError) {
    return (
      <FatalScreen
        title="Could not connect"
        message="CoopMS could not reach your cooperative. Check your internet connection and try again — your sign-in is still saved."
        details={startupError}
        onRetry={() => {
          setStartupError('');
          setBootAttempt((value) => value + 1);
        }}
        secondaryLabel="Start over with a different cooperative"
        onSecondary={() => {
          setStartupError('');
          void changeCooperative();
          setBootAttempt((value) => value + 1);
        }}
      />
    );
  }

  if (!hasTenant) {
    return <TenantScreen onReady={() => { setHasTenant(true); setCoopName(getCoopName()); }} />;
  }

  if (!token || !dashboard) {
    if (token && dashboardError) {
      return (
        <FatalScreen
          title="Dashboard unavailable"
          message="Your sign-in is still saved, but CoopMS could not load your dashboard. Check your internet connection and try again."
          details={dashboardError}
          onRetry={() => setBootAttempt((value) => value + 1)}
        />
      );
    }
    return <LoginScreen onLogin={onLogin} coopName={coopName} onChangeCooperative={changeCooperative} />;
  }

  return (
    <SafeAreaView style={styles.appRoot}>
      <StatusBar barStyle="dark-content" />
      <Header title={title} onLogout={logout} />
      {activeTab === 'home' ? <HomeScreen data={dashboard} reload={() => reload()} setActive={setActiveTab} /> : null}
      {activeTab === 'profile' ? <ProfileScreen token={token} data={dashboard} reload={() => reload()} /> : null}
      {activeTab === 'savings' ? <SavingsScreen token={token} /> : null}
      {activeTab === 'loans' ? <LoansScreen token={token} dashboard={dashboard} reload={() => reload()} /> : null}
      {activeTab === 'ctas' ? <CtasScreen token={token} /> : null}
      {activeTab === 'notifications' ? <NotificationsScreen token={token} initial={dashboard.notifications} /> : null}
      <TabBar active={activeTab} setActive={setActiveTab} unread={dashboard.summary.unread_notifications} showCtas={ctasEnabled} />
    </SafeAreaView>
  );
}

export default function App() {
  return (
    <AppErrorBoundary>
      <AppContent />
    </AppErrorBoundary>
  );
}

const styles = StyleSheet.create({
  appRoot: { flex: 1, backgroundColor: BG },
  loadingRoot: { flex: 1, alignItems: 'center', justifyContent: 'center', backgroundColor: BG },
  loadingTitle: { color: NAVY, fontSize: 18, fontWeight: '900', marginTop: 18 },
  loadingHint: { color: MUTED, fontSize: 13, marginTop: 6, textAlign: 'center', paddingHorizontal: 28 },
  fatalRoot: { flex: 1, alignItems: 'center', justifyContent: 'center', backgroundColor: NAVY, padding: 20 },
  fatalCard: { width: '100%', backgroundColor: '#fff', borderRadius: 16, padding: 18, borderWidth: 1, borderColor: BORDER },
  fatalTitle: { color: NAVY, fontSize: 22, fontWeight: '900', marginBottom: 10 },
  fatalMessage: { color: INK, fontSize: 15, lineHeight: 21, marginBottom: 12 },
  fatalSecondary: { alignItems: 'center', paddingVertical: 12, marginTop: 4 },
  fatalSecondaryText: { color: MUTED, fontSize: 14, fontWeight: '600' },
  fatalDetails: { color: '#D93030', backgroundColor: '#FFF1F1', borderRadius: 8, padding: 10, marginBottom: 14, fontSize: 12, lineHeight: 17 },
  authRoot: { flex: 1, backgroundColor: NAVY },
  authContent: { flex: 1, justifyContent: 'center', padding: 24 },
  logoMark: { width: 72, height: 72, borderRadius: 20, backgroundColor: '#fff', alignItems: 'center', justifyContent: 'center', marginBottom: 18, borderWidth: 1, borderColor: 'rgba(255,255,255,0.38)' },
  brand: { color: '#fff', fontSize: 34, fontWeight: '900', letterSpacing: 0 },
  tagline: { color: '#DDE8FF', fontSize: 15, marginTop: 6, marginBottom: 28 },
  loginCard: { backgroundColor: '#fff', borderRadius: 18, padding: 20, shadowColor: '#061E4A', shadowOpacity: 0.2, shadowRadius: 22, elevation: 5 },
  loginTitle: { color: INK, fontSize: 20, fontWeight: '800', marginBottom: 14 },
  helperText: { color: MUTED, fontSize: 13, marginBottom: 12, marginTop: -6, lineHeight: 19 },
  input: { minHeight: 50, borderWidth: 1, borderColor: BORDER, borderRadius: 12, padding: 14, marginBottom: 12, fontSize: 16, color: INK, backgroundColor: '#FBFCFE' },
  inputLight: { minHeight: 48, borderWidth: 1, borderColor: BORDER, borderRadius: 12, padding: 12, marginTop: 6, marginBottom: 12, fontSize: 15, color: INK, backgroundColor: '#fff' },
  primaryButton: { minHeight: 50, backgroundColor: BLUE, borderRadius: 12, padding: 15, alignItems: 'center', justifyContent: 'center' },
  primaryButtonText: { color: '#fff', fontWeight: '800', fontSize: 16 },
  secondaryButton: { minHeight: 48, backgroundColor: '#EEF4FF', borderRadius: 12, padding: 14, alignItems: 'center', marginTop: 4, marginBottom: 10 },
  secondaryButtonText: { color: BLUE, fontWeight: '900' },
  textButton: { alignItems: 'center', paddingVertical: 12 },
  resetPanel: { borderWidth: 1, borderColor: '#D7E7FF', backgroundColor: '#F4F8FF', borderRadius: 14, padding: 12, marginTop: 2, marginBottom: 8 },
  dangerButton: { minHeight: 48, backgroundColor: '#D93030', borderRadius: 12, padding: 14, alignItems: 'center', marginTop: 4 },
  dangerButtonText: { color: '#fff', fontWeight: '800' },
  disabled: { opacity: 0.7 },
  apiHint: { color: MUTED, marginTop: 12, fontSize: 12 },
  header: { paddingHorizontal: 18, paddingVertical: 14, backgroundColor: '#fff', flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', borderBottomWidth: 1, borderColor: '#E4E9F0' },
  headerEyebrow: { color: YELLOW, fontWeight: '900', fontSize: 12 },
  headerTitle: { color: NAVY, fontWeight: '900', fontSize: 24 },
  iconButton: { width: 46, height: 46, borderRadius: 14, backgroundColor: '#EEF4FF', alignItems: 'center', justifyContent: 'center' },
  screen: { padding: 14, paddingBottom: 104 },
  memberBand: { backgroundColor: NAVY, borderRadius: 18, padding: 18, flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', shadowColor: '#061E4A', shadowOpacity: 0.18, shadowRadius: 18, elevation: 3 },
  memberGreeting: { color: YELLOW, fontSize: 12, fontWeight: '900', textTransform: 'uppercase', marginBottom: 5 },
  memberName: { color: '#fff', fontSize: 20, fontWeight: '900' },
  memberMeta: { color: '#CFE0FF', marginTop: 4 },
  certifiedBadge: { backgroundColor: YELLOW, borderRadius: 999, paddingHorizontal: 10, paddingVertical: 7, flexDirection: 'row', gap: 4, alignItems: 'center' },
  certifiedText: { color: NAVY, fontWeight: '900', fontSize: 12 },
  statGrid: { flexDirection: 'row', flexWrap: 'wrap', gap: 10, marginVertical: 14 },
  statCard: { flexBasis: '48%', flexGrow: 1, minHeight: 116, backgroundColor: '#fff', borderRadius: 16, padding: 14, borderWidth: 1, borderColor: '#E2E8F0' },
  statYellow: { borderColor: '#F5D66D', backgroundColor: '#FFF8E5' },
  statGreen: { borderColor: '#A7E0C2', backgroundColor: '#EDFFF4' },
  statTopLine: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', gap: 8 },
  statLabel: { color: MUTED, fontSize: 12, fontWeight: '800', textTransform: 'uppercase' },
  statValue: { color: INK, fontSize: 17, fontWeight: '900', marginTop: 8 },
  statHint: { color: MUTED, fontSize: 12, marginTop: 8, fontWeight: '700' },
  card: { backgroundColor: '#fff', borderRadius: 16, marginTop: 14, borderWidth: 1, borderColor: '#E2E8F0', overflow: 'hidden', padding: 14 },
  cardTitle: { fontSize: 17, color: INK, fontWeight: '900', marginBottom: 8 },
  rowBetween: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center' },
  listRow: { paddingVertical: 12, borderTopWidth: 1, borderColor: '#EEF2F6' },
  rowTitle: { color: INK, fontWeight: '800', fontSize: 15 },
  rowSub: { color: MUTED, marginTop: 4, lineHeight: 19 },
  dateText: { color: MUTED, fontSize: 12, fontWeight: '800' },
  emptyText: { color: MUTED, paddingVertical: 12 },
  actionNotice: { borderWidth: 1, borderColor: '#D7E7FF', backgroundColor: '#F4F8FF', borderRadius: 14, padding: 13, marginTop: 4 },
  quickAction: { minHeight: 68, flexDirection: 'row', alignItems: 'center', gap: 12, borderTopWidth: 1, borderColor: '#EEF2F6', paddingVertical: 12 },
  quickActionYellow: { backgroundColor: '#FFF8E5', borderRadius: 14, borderTopWidth: 0, paddingHorizontal: 10, marginBottom: 4 },
  quickIcon: { width: 42, height: 42, borderRadius: 12, alignItems: 'center', justifyContent: 'center', backgroundColor: '#EEF4FF' },
  quickText: { flex: 1 },
  quickLabel: { color: INK, fontWeight: '900', fontSize: 15 },
  quickHelper: { color: MUTED, marginTop: 3, fontSize: 12, lineHeight: 17 },
  pressed: { opacity: 0.72 },
  progressShell: { height: 10, borderRadius: 999, backgroundColor: '#E5EAF2', overflow: 'hidden', marginTop: 10, marginBottom: 6 },
  progressFill: { height: 10, backgroundColor: YELLOW },
  fieldBlock: { marginTop: 6 },
  fieldLabel: { color: '#667085', fontWeight: '800', textTransform: 'capitalize' },
  optionWrap: { flexDirection: 'row', flexWrap: 'wrap', gap: 8, marginTop: 8, marginBottom: 10 },
  optionStack: { gap: 8, marginTop: 8, marginBottom: 10 },
  privacyLink: { flexDirection: 'row', alignItems: 'center', justifyContent: 'center', gap: 6, paddingVertical: 18 },
  buildStamp: { textAlign: 'center', color: '#9AA7B8', fontSize: 11, paddingBottom: 22 },
  privacyLinkText: { color: MUTED, fontSize: 13, fontWeight: '600' },
  payInRow: { flexDirection: 'row', alignItems: 'center', gap: 12, marginTop: 10 },
  payInNumber: { fontSize: 24, fontWeight: '900', color: INK, letterSpacing: 1 },
  copyButton: { flexDirection: 'row', alignItems: 'center', gap: 6, borderWidth: 1, borderColor: BORDER, borderRadius: 999, paddingHorizontal: 14, paddingVertical: 9, backgroundColor: '#fff' },
  copyButtonText: { color: BLUE, fontWeight: '800', fontSize: 13 },
  optionChip: { borderWidth: 1, borderColor: '#D7DEE8', backgroundColor: '#fff', borderRadius: 999, paddingHorizontal: 12, paddingVertical: 9 },
  optionChipSelected: { borderColor: BLUE, backgroundColor: '#EEF4FF' },
  optionChipText: { color: '#667085', fontWeight: '800' },
  optionChipTextSelected: { color: BLUE },
  collateralOption: { borderWidth: 1, borderColor: '#D7DEE8', borderRadius: 8, backgroundColor: '#fff', padding: 12 },
  collateralSelected: { borderColor: BLUE, backgroundColor: '#F4F8FF' },
  selectionCount: { color: BLUE, fontSize: 12, fontWeight: '900' },
  selectedWrap: { flexDirection: 'row', flexWrap: 'wrap', gap: 8, marginTop: 10, marginBottom: 8 },
  selectedPill: { flexDirection: 'row', alignItems: 'center', gap: 6, backgroundColor: '#FFF8E5', borderWidth: 1, borderColor: '#F5D66D', borderRadius: 999, paddingHorizontal: 10, paddingVertical: 7 },
  selectedPillText: { color: NAVY, fontWeight: '900', fontSize: 12 },
  searchBox: { minHeight: 48, flexDirection: 'row', alignItems: 'center', gap: 8, borderWidth: 1, borderColor: BORDER, backgroundColor: '#FBFCFE', borderRadius: 12, paddingHorizontal: 12, marginTop: 10, marginBottom: 8 },
  searchInput: { flex: 1, color: INK, fontSize: 14, paddingVertical: 10 },
  searchResults: { gap: 8, marginTop: 4, marginBottom: 10 },
  guarantorChoice: { minHeight: 64, flexDirection: 'row', alignItems: 'center', gap: 10, borderWidth: 1, borderColor: '#D7DEE8', borderRadius: 8, backgroundColor: '#fff', padding: 10 },
  guarantorSelected: { borderColor: BLUE, backgroundColor: '#F4F8FF' },
  guarantorCheck: { width: 24, height: 24, borderRadius: 999, borderWidth: 1, borderColor: BLUE, backgroundColor: '#fff', alignItems: 'center', justifyContent: 'center' },
  guarantorCheckSelected: { backgroundColor: BLUE },
  detailLine: { color: INK, marginBottom: 8, fontWeight: '600' },
  disclaimer: { color: '#667085', fontSize: 12, lineHeight: 18, marginBottom: 12 },
  previewBox: { borderWidth: 1, borderColor: '#D7E7FF', backgroundColor: '#F4F8FF', borderRadius: 8, padding: 12, marginBottom: 10 },
  statusPill: { color: '#fff', backgroundColor: '#7D8797', overflow: 'hidden', borderRadius: 999, paddingHorizontal: 9, paddingVertical: 4, fontSize: 12, textTransform: 'capitalize' },
  statusActive: { backgroundColor: '#159A5B' },
  linkText: { color: BLUE, fontWeight: '800' },
  tabBar: { position: 'absolute', bottom: 0, left: 0, right: 0, height: 82, backgroundColor: '#fff', flexDirection: 'row', borderTopWidth: 1, borderColor: '#E4E9F0', paddingBottom: 10, shadowColor: '#061E4A', shadowOpacity: 0.08, shadowRadius: 16, elevation: 8 },
  tabButton: { flex: 1, minHeight: 54, alignItems: 'center', justifyContent: 'center', gap: 4 },
  tabLabel: { color: '#7D8797', fontSize: 11, fontWeight: '700' },
  tabActive: { color: NAVY },
  dot: { position: 'absolute', top: 15, right: 22, width: 8, height: 8, borderRadius: 999, backgroundColor: '#D93030' }
});
