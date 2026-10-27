export type MobileUser = {
  id: number;
  username: string;
  role: string;
  email?: string;
};

export type ProfileCompletion = {
  percent: number;
  missing_fields: string[];
  certified_member: boolean;
};

export type MobileMember = {
  id: number;
  member_number: string;
  full_name: string;
  first_name: string;
  last_name: string;
  email: string;
  phone: string;
  status: string;
  total_savings: number;
  share_capital: number;
  loan_eligibility_amount: number;
  loan_type_limits?: Record<string, { label?: string; tiers?: { max_months: number; rate: number }[]; max_amount: number; eligible_amount: number; max_tenure_months: number }>;
  profile_completion: ProfileCompletion;
};

export type SavingRow = {
  id: number;
  amount: number;
  month: string;
  payment_type?: string;
  payment_method?: string;
  receipt_number?: string;
  date?: string;
};

export type Loan = {
  id: number;
  loan_number: string;
  amount: number;
  purpose: string;
  tenure: number;
  interest_rate: number;
  total_repayment: number;
  balance: number;
  status: string;
  approval_stage?: string;
  is_disbursed: boolean;
  date_applied?: string;
  withdrawal_reason?: string;
  schedule?: LoanScheduleRow[];
};

export type LoanPreview = {
  success: boolean;
  amount: number;
  purpose: string;
  tenure: number;
  interest_rate: number;
  interest_method: string;
  monthly_payment: number;
  total_repayment: number;
  total_interest: number;
  schedule?: LoanScheduleRow[];
};

export type LoanScheduleRow = {
  month: number;
  payment: number;
  principal: number;
  interest: number;
  balance: number;
};

export type LoanPurposeOption = {
  value: string;
  label: string;
  interest_rate: number;
  interest_method: string;
};

export type CollateralOption = {
  value: string;
  label: string;
  description: string;
};

export type GuarantorOption = {
  id: number;
  member_number: string;
  full_name: string;
  email: string;
  phone: string;
  total_savings: number;
};

export type LoanOptionsPayload = {
  success: boolean;
  purposes: LoanPurposeOption[];
  collateral_options: CollateralOption[];
  guarantors_required: number;
  eligible_guarantors: GuarantorOption[];
  max_tenure_months: number;
  loan_eligibility_amount: number;
  staff_member: boolean;
};

export type MobileNotification = {
  id: number;
  title: string;
  message: string;
  notification_type: string;
  is_read: number;
  action_url?: string;
  created_at?: string;
};

export type DashboardPayload = {
  success: boolean;
  member: MobileMember;
  summary: {
    unread_notifications: number;
    active_loan_balance: number;
  };
  savings: SavingRow[];
  loans: Loan[];
  recent_transactions: Array<Record<string, unknown>>;
  notifications: MobileNotification[];
};

export type CtasSubscription = {
  id: number;
  cycle_name: string;
  target_amount: number;
  tenure_months: number;
  monthly_deduction: number;
  status: string;
  payout_month: number | null;
  total_recovered: number;
  outstanding: number;
  arrears_amount: number;
  progress: number;
};

export type CtasCycle = {
  id: number;
  name: string;
  duration_months: number;
  affordability_method: string;
  savings_multiple: number;
};

export type CtasPayload = {
  success: boolean;
  enabled: boolean;
  savings_balance?: number;
  has_active?: boolean;
  subscriptions: CtasSubscription[];
  open_cycles: CtasCycle[];
};

export type PayInAccount = {
  account_number: string;
  account_name: string;
  bank_name: string;
};

export type PayInPayload = {
  success: boolean;
  enabled: boolean;
  account: PayInAccount | null;
  preference: string;
  choices: { key: string; label: string }[];
};
