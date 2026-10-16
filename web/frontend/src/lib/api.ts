function getBasePath(): string {
  const path = window.location.pathname;
  const cleaned = path.replace(/\/index\.html$/, '').replace(/\/$/, '');
  return cleaned;
}

const BASE = getBasePath();

async function request<T>(url: string, options?: RequestInit): Promise<T> {
  // 全部是动态应用接口（登录态/数据等），禁止浏览器 HTTP 缓存——否则 /api/accounts 等可能被启发式
  // 缓存住旧响应（曾表现为“扫码登录后卡片仍显示未登录”）。调用方可用 options.cache 覆盖。
  const res = await fetch(`${BASE}${url}`, { cache: 'no-store', ...options });
  if (!res.ok) {
    // 优先显示后端返回的实质错误信息（FastAPI 的 {detail}），而不是无意义的 "API error: 400"
    let detail = '';
    try {
      const j = await res.json();
      detail = (j && (j.detail || j.message)) || '';
    } catch {
      /* 响应体不是 JSON，忽略 */
    }
    throw new Error(detail || `请求失败（${res.status} ${res.statusText}）`);
  }
  return res.json() as Promise<T>;
}

export interface StatusResponse {
  gateway: boolean;
  transport?: string;
  runtime?: string;
  skills: SkillItem[];
  personas: PersonaItem[];
}

export interface HarnessSettingsResponse {
  runtime: string;
  models: Record<string, string>;
  sandbox: string;
  codex_auto_approve: boolean;
  locked: boolean;
  agents: { id: string; label: string; protocol: string; installed: boolean; install_hint: string }[];
}

export function fetchHarnessSettings(): Promise<HarnessSettingsResponse> {
  return request('/api/settings/harness');
}

export interface HarnessModel {
  id: string;
  label: string;
  default: boolean;
}

export function fetchHarnessModels(runtime: string): Promise<{ runtime: string; models: HarnessModel[] }> {
  return request(`/api/settings/harness/models/${encodeURIComponent(runtime)}`, { method: 'POST' });
}

export function saveHarnessSettings(runtime: string, model: string, sandbox: string, codexAutoApprove?: boolean): Promise<HarnessSettingsResponse> {
  return request('/api/settings/harness', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ runtime, model, sandbox, codex_auto_approve: codexAutoApprove }),
  });
}

export interface PersonaItem {
  name: string;
  description: string;
}

export interface PersonaDetail {
  name: string;
  content: string;
}

export interface SkillItem {
  name: string;
  description: string;
  layer: string;
  needsApi: boolean;
  apiConfigured: boolean;
}

export interface ApiKeySpec {
  env: string;
  label: string;
  required: boolean;
  secret: boolean;
  configured: boolean;
  masked: string;   // 脱敏值或非 secret 明文
  choices: string[];
}

export interface ApiProviderSpec {
  id: string;
  name: string;
  keys: ApiKeySpec[];
}

export interface ApiSpec {
  label: string;
  settings: ApiKeySpec[];
  providers: ApiProviderSpec[];
}

export interface SkillDetail {
  name: string;
  layer: string;
  description: string;
  body: string;
  needsApi: boolean;
  apiConfigured: boolean;
  apiSpec: ApiSpec | null;
}

export type FileKind = 'text' | 'image' | 'video' | 'audio' | 'binary';

/** 顶层项目目录的 .easel.json 展示头（供内容库富展示）。 */
export interface OutputMeta {
  title?: string;
  summary?: string;
  platform?: string;
  kind?: string;             // article|xhs-note|video|cards|poster|audio|other
  status?: string;           // draft|ready|published
  tags?: string[];
  cover?: string;            // outputs 下相对路径（后端已解析存在性）
  deliverables?: string[];   // 成品文件名（项目根相对）
  deliverablePaths?: string[];  // 成品的 outputs 相对路径（后端已解析存在性）
}

/** 产物树节点：文件或目录（目录带 children，可无限嵌套点开）。 */
export interface OutputNode {
  name: string;
  type: 'dir' | 'file';
  path: string;              // outputs 下的相对路径，如 "short-drama/监控诡影/episodes/ep01"
  mtime?: number;
  kind?: FileKind;           // 仅 file
  size?: number;             // 仅 file
  children?: OutputNode[];   // 仅 dir
  fileCount?: number;        // 仅 dir：递归文件数
  meta?: OutputMeta;         // 仅顶层项目 dir
}

/** @deprecated 用 OutputNode（type==='file'）。保留别名减少改动面。 */
export type OutputFile = OutputNode;

export interface OutputContent {
  path: string;
  content: string;
  kind: FileKind;
  isBinary: boolean;
}

export interface SkillResponse {
  response: string;
}

export interface ProfileBuildResponse {
  created: boolean;
  name: string;
  async?: boolean;
  status?: string;
  log?: string;
}

export interface ProfileBuildStatus {
  state: 'running' | 'done' | 'failed' | 'unknown';
  log: string;
}

export function fetchStatus(): Promise<StatusResponse> {
  return request<StatusResponse>('/api/status');
}

export function fetchPersonas(): Promise<PersonaItem[]> {
  return request<PersonaItem[]>('/api/personas');
}

export function fetchPersonaDetail(name: string): Promise<PersonaDetail> {
  return request<PersonaDetail>(`/api/persona/${encodeURIComponent(name)}`);
}

export interface PersonaFile {
  filename: string;
  content: string;
}

export function fetchPersonaFiles(name: string): Promise<{ name: string; files: PersonaFile[] }> {
  return request(`/api/persona/${encodeURIComponent(name)}/files`);
}

export function savePersonaFile(name: string, filename: string, content: string): Promise<{ ok: boolean }> {
  return request(`/api/persona/${encodeURIComponent(name)}/file`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ filename, content }),
  });
}

export function deletePersona(name: string): Promise<{ ok: boolean; deleted: string }> {
  return request(`/api/persona/${encodeURIComponent(name)}`, { method: 'DELETE' });
}

// ---- 热点雷达 ----
export interface TrendItem { title: string; hot: string; url: string; }
export interface TrendGroup { platform: string; label: string; items: TrendItem[]; ok?: boolean; }
export function fetchTrends(platforms: string, limit = 12): Promise<{ trends: TrendGroup[]; updated: number }> {
  return request(`/api/trends?platforms=${encodeURIComponent(platforms)}&limit=${limit}`);
}

// ---- 内容排期 ----
export interface ScheduleItem {
  id: string; title: string; date: string; platform: string;
  time: string; status: string; note: string;
  kind?: string;          // content（内容/发布）| event（平台活动/节日/特殊日期）
  url?: string;           // 已发布链接
  source?: string;        // manual | publish-page | chat | scheduler
  event_type?: string;    // event 专属：节日/电商/平台活动/行业
  end_date?: string;      // event 专属：活动区间结束日
}
export type ScheduleInput = Omit<ScheduleItem, 'id'>;
export interface ScheduleContext {
  today?: string; window_days?: number; published_recent?: number;
  per_platform?: { platform: string; recent_count: number; last_date: string | null; days_since_last: number | null }[];
  upcoming_schedules?: ScheduleItem[];
  upcoming_events?: ScheduleItem[];
  suggestions?: string[];
}
export function fetchSchedule(): Promise<ScheduleItem[]> {
  return request('/api/schedule');
}
export function fetchScheduleContext(days = 14): Promise<ScheduleContext> {
  return request(`/api/schedule/context?days=${days}`);
}
export function createSchedule(item: ScheduleInput): Promise<ScheduleItem> {
  return request('/api/schedule', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(item),
  });
}
export function updateSchedule(id: string, item: ScheduleInput): Promise<ScheduleItem> {
  return request(`/api/schedule/${encodeURIComponent(id)}`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(item),
  });
}
export function deleteSchedule(id: string): Promise<{ ok: boolean }> {
  return request(`/api/schedule/${encodeURIComponent(id)}`, { method: 'DELETE' });
}

// ---- 选题库 ----
export interface Idea {
  id: string; title: string; note: string; source: string; status: string; created: number;
}
export type IdeaInput = { title: string; note?: string; source?: string; status?: string };
export function fetchIdeas(): Promise<Idea[]> { return request('/api/ideas'); }
export function createIdea(item: IdeaInput): Promise<Idea> {
  return request('/api/ideas', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(item) });
}
export function updateIdea(id: string, item: IdeaInput): Promise<Idea> {
  return request(`/api/ideas/${encodeURIComponent(id)}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(item) });
}
export function deleteIdea(id: string): Promise<{ ok: boolean }> {
  return request(`/api/ideas/${encodeURIComponent(id)}`, { method: 'DELETE' });
}

export function fetchSkills(): Promise<SkillItem[]> {
  return request<SkillItem[]>('/api/skills');
}

export function fetchSkillDetail(name: string): Promise<SkillDetail> {
  return request<SkillDetail>(`/api/skill/${encodeURIComponent(name)}`);
}

/** 保存 API key 到项目根 .env（仅注册表内变量）。返回各需 API 的 skill 是否已就绪。 */
export function saveEnv(updates: Record<string, string>): Promise<{ ok: boolean; skills: Record<string, boolean> }> {
  return request('/api/env', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ updates }),
  });
}

export function executeSkill(skill: string, input: string, persona?: string): Promise<SkillResponse> {
  return request<SkillResponse>('/api/skill', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ skill, input, persona: persona || undefined }),
  });
}

/** 一次性 agent 任务（返回整段 markdown）——用于一稿多改 / 爆款拆解 / 发布预检等工具型调用。 */
export function runAgent(message: string, persona?: string): Promise<{ response: string }> {
  return request('/api/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, persona: persona || undefined }),
  });
}

/** 从表单构建画像（首次引导）。后端写基线 + agent 分析社媒链接增强。 */
export function buildProfile(name: string, form: Record<string, unknown>): Promise<ProfileBuildResponse> {
  return request<ProfileBuildResponse>('/api/profile/build', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, form }),
  });
}

/** 轮询画像 AI 增强进度（构建异步化后用）。 */
export function profileBuildStatus(name: string): Promise<ProfileBuildStatus> {
  return request<ProfileBuildStatus>(`/api/profile/build/status/${encodeURIComponent(name)}`);
}

export function fetchOutputs(): Promise<OutputNode[]> {
  return request<OutputNode[]>('/api/outputs');
}

export function fetchArtifactArchive(path: string, signal?: AbortSignal): Promise<{ paths: string[] }> {
  return request<{ paths: string[] }>(`/api/artifacts/archive?path=${encodeURIComponent(path)}`, { signal });
}

export function fetchOutputContent(path: string): Promise<OutputContent> {
  return request<OutputContent>(`/api/output/${path.split('/').map(encodeURIComponent).join('/')}`);
}

/** 媒体文件（图片/视频/音频/HTML/PDF）的原样 URL，用于 <img>/<video>/iframe/下载。 */
export function mediaUrl(path: string): string {
  return `${BASE}/api/media/${path.split('/').map(encodeURIComponent).join('/')}`;
}

/** 删除内容库里的文件或整个项目目录（系统数据受保护，后端会拒）。 */
export function deleteOutput(path: string): Promise<{ ok: boolean; deleted: string }> {
  return request(`/api/output/${path.split('/').map(encodeURIComponent).join('/')}`, { method: 'DELETE' });
}

export interface UploadedFile { id: string; name: string; path: string; }

/** OpenClaw ask_user 问答题（SSE question 事件 payload）。 */
export interface ChatQuestionOption { label: string; description?: string; }
export interface ChatQuestionItem {
  questionId: string;
  header?: string;
  question: string;
  options?: ChatQuestionOption[];
  multiSelect?: boolean;
}
export interface ChatQuestion {
  id: string;              // gateway question record id (ask_...)
  questions: ChatQuestionItem[];
  expiresAtMs?: number;
}
export async function answerQuestion(
  payload: { questionId: string; answers: Record<string, string[]>; resolvedBy?: string },
): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(`${BASE}/api/chat/question/answer`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await res.json().catch(() => ({})) as { ok?: boolean; error?: string };
  return { ok: data.ok === true, error: data.error };
}

/** 批量查 question 状态：过滤重放中已解决/已过期的旧题。 */
export async function questionStatus(
  questionIds: string[],
): Promise<Record<string, { status: string }>> {
  if (!questionIds.length) return {};
  try {
    const res = await fetch(`${BASE}/api/chat/question/status`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ questionIds }),
    });
    const data = await res.json().catch(() => ({})) as { ok?: boolean; questions?: Record<string, { status: string }> };
    return data.questions || {};
  } catch {
    return {};
  }
}
/** 上传素材到当前会话的隔离 inbox，返回后端可校验的附件引用。 */
export async function uploadFiles(files: File[], sessionId: string): Promise<UploadedFile[]> {
  const fd = new FormData();
  for (const f of files) fd.append('files', f);
  fd.append('sessionId', sessionId);
  const r = await request<{ ok: boolean; files: UploadedFile[] }>('/api/upload', { method: 'POST', body: fd });
  return r.files;
}

/** 超限文件复制通道：返回与 uploadFiles 同构的附件引用。 */
export interface AdoptedFile { id: string; name: string; path: string; }
export async function adoptOversize(files: File[], sessionId: string): Promise<AdoptedFile[]> {
  const fd = new FormData();
  for (const f of files) fd.append('files', f);
  fd.append('sessionId', sessionId);
  const r = await request<{ ok: boolean; files: AdoptedFile[] }>('/api/upload/local', { method: 'POST', body: fd });
  return r.files;
}

export function deleteSession(sessionKey: string): Promise<{ deleted: boolean }> {
  return request<{ deleted: boolean }>(`/api/session/${encodeURIComponent(sessionKey)}`, {
    method: 'DELETE',
  });
}

// ---- 账号登录 ----
export interface AccountItem {
  platform: string;
  name: string;
  backend: string;      // xhs | web | biliup | unsupported
  supported: boolean;
  loggedIn: boolean;
  note: string;
}

export interface LoginStart {
  mode: 'qr' | 'terminal' | 'credentials';   // credentials = 凭证式（公众号 AppID/AppSecret）
  state?: string;       // starting | qr_ready | success | expired | error | unknown
  message?: string;
  qr?: string;          // outputs 下相对路径，用 mediaUrl() 取图
  qrTs?: number;
  configured?: boolean; // 凭证式：是否已配置
}

// ---- 凭证式账号（微信公众号 AppID/AppSecret）----
export interface CredentialStatus {
  configured: boolean;
  appIdMasked: string;
  name: string;
  author: string;
}

/** 读取凭证式平台（公众号）已配置状态（AppSecret 不回传）。 */
export function getCredentials(platform: string): Promise<CredentialStatus> {
  return request<CredentialStatus>(`/api/accounts/${encodeURIComponent(platform)}/credentials`);
}

/** 保存公众号 AppID/AppSecret（后端会调官方接口验证连通性）。 */
export function saveCredentials(
  platform: string,
  payload: { appId: string; appSecret: string; name?: string; author?: string },
): Promise<{ ok: boolean; message: string }> {
  return request(`/api/accounts/${encodeURIComponent(platform)}/credentials`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

/** 启动「公众号后台」扫码登录（数据中心取数用，独立于 AppID 凭证）。返回二维码状态。 */
export function startMpLogin(platform: string): Promise<LoginStatus> {
  return request<LoginStatus>(`/api/accounts/${encodeURIComponent(platform)}/mp-login`, { method: 'POST' });
}

/** 轮询公众号后台扫码登录状态。 */
export function mpLoginStatus(platform: string): Promise<LoginStatus> {
  return request<LoginStatus>(`/api/accounts/${encodeURIComponent(platform)}/mp-login/status`,
    { signal: AbortSignal.timeout(8000) });
}

export interface LoginStatus {
  mode: 'qr';
  state: string;
  message: string;
  qr: string;
  qrTs?: number;
}

export function fetchAccounts(): Promise<AccountItem[]> {
  return request<AccountItem[]>('/api/accounts');
}

export interface AccountWhoami {
  loggedIn: boolean;
  name: string;
  avatar: string;   // 头像 URL（http）或空
}

/** 小红书会话健康：主站（读数据）与创作平台（发布）是两套独立会话。 */
export interface AccountSession {
  platform: string;
  cookie: BenchCookieStatus | null;
  web: { ok: boolean; detail: string; nickname?: string; user_id?: string } | null;
  creator: { cookie_present: boolean; detail: string } | null;
  logged_in?: boolean;
  checked_at?: number;
  name?: string;
  hint?: string;
  error?: string;
}

/** 查会话健康；check=1 会用 cookie 走一次纯 HTTP 校验（约 1 秒，不起浏览器）。 */
export function accountSession(platform: string, check = false): Promise<AccountSession> {
  return request<AccountSession>(
    `/api/accounts/${encodeURIComponent(platform)}/session${check ? '?check=1' : ''}`);
}

/** 真校验某平台登录态 + 拉昵称/头像（后端起 headless 浏览器，数秒）。 */
export function accountWhoami(platform: string, force = false): Promise<AccountWhoami> {
  return request<AccountWhoami>(`/api/accounts/${encodeURIComponent(platform)}/whoami${force ? '?force=true' : ''}`);
}

/** 退出登录：删该平台持久化登录态。 */
export function logoutAccount(platform: string): Promise<{ ok: boolean; deleted: string[] }> {
  return request(`/api/logout/${encodeURIComponent(platform)}`, { method: 'POST' });
}

export interface PublishResult {
  ok?: boolean;
  message: string;
  detail?: string;
  async?: boolean;   // true = 异步发布（抖音，可能触发短信验证），需轮询 publishStatus
  pending?: boolean;
}

/** 一键发布到某平台（真发布，--exec）。media 为 outputs 相对路径数组。
 * 抖音返回 {async:true}，需轮询 publishStatus；其他平台同步返回结果。 */
export function publishNow(
  platform: string,
  payload: { title: string; body: string; media: string[]; tags?: string },
): Promise<PublishResult> {
  return request<PublishResult>(`/api/publish/${encodeURIComponent(platform)}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

export interface PublishStatus {
  mode: 'publish';
  state: string;     // starting | sms_required | verifying | success | error | unknown
  message: string;
}

/** 轮询异步发布状态（抖音）。 */
export function publishStatus(platform: string): Promise<PublishStatus> {
  return request<PublishStatus>(`/api/publish/${encodeURIComponent(platform)}/status`);
}

/** 发布触发短信墙时回填验证码。 */
export function submitPublishSms(platform: string, code: string): Promise<{ ok: boolean }> {
  return request(`/api/publish/${encodeURIComponent(platform)}/sms`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ code }),
  });
}

export function startLogin(platform: string): Promise<LoginStart> {
  return request<LoginStart>(`/api/login/${encodeURIComponent(platform)}?restart=true`, { method: 'POST' });
}

export function loginStatus(platform: string): Promise<LoginStatus> {
  return request<LoginStatus>(`/api/login/${encodeURIComponent(platform)}/status`,
    { signal: AbortSignal.timeout(8000) });
}

// ---- 归因层：账号创作数据 ----
export interface AnalyticsPlatform {
  platform: string;
  name: string;
  loggedIn: boolean;
}

export interface AccountAnalytics {
  platform: string;
  name: string;
  nickname: string;
  loggedIn: boolean;
  followers: number | null;
  likes: number | null;
  following: number | null;
  posts: number | null;
  metrics: { label: string; value: string | number; vs: string }[];
  notes: { title: string; url: string; cover?: string; stat?: string }[];
  growth: Record<'last' | 'day' | 'week' | 'month' | 'year',
    { followers: number | null; likes: number | null; posts: number | null; since_days: number | null } | null>;
  fetched_at: number;
  warning?: string;
}

/** 支持抓数据的平台 + 各自登录态。 */
export function fetchAnalyticsPlatforms(): Promise<AnalyticsPlatform[]> {
  return request<AnalyticsPlatform[]>('/api/analytics/platforms');
}

/** 抓某平台已登录账号的创作数据，客户端等待时限略长于后端。 */
export function fetchAccountAnalytics(platform: string): Promise<AccountAnalytics> {
  return request<AccountAnalytics>(`/api/analytics/${encodeURIComponent(platform)}`,
    { signal: AbortSignal.timeout(platform === 'wechat-oa' ? 195000 : 105000) });
}

/** 回填短信验证码（登录风控短信墙）：提交后 runner 读走填码继续登录。 */
export function submitLoginSms(platform: string, code: string): Promise<{ ok: boolean }> {
  return request(`/api/login/${encodeURIComponent(platform)}/sms`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ code }),
  });
}

// ---- 对标账号（他人公开账号，与「自有账号」分开） ----
/** 一个指标：平台原始展示串 + 解析值 + 是否只是量级下界。 */
export interface BenchMetric {
  raw: string;
  num: number | null;
  lower_bound: boolean;
}

/** 平台适配器能力声明：决定哪些字段「本来就没有」而不是「这次没取到」。 */
export interface BenchPlatformFields {
  followers?: boolean;
  likes?: boolean;
  favorites?: boolean;
  comments?: boolean;
  posts?: boolean;
  cover?: boolean;
  work_likes?: boolean;
  work_favorites?: boolean;
  work_comments?: boolean;
  work_published_at?: boolean;
}

export interface BenchPlatform {
  platform: string;
  name: string;
  auto: boolean;
  experimental: boolean;
  id_label: string;
  id_hint: string;
  fields: BenchPlatformFields;
  note: string;
}

export interface BenchWork {
  id: string;
  title: string;
  url: string;
  cover_url: string;
  cover: string;
  cover_path: string;
  published_at_ms: number | null;
  likes: number | null;
  likes_raw: string;
  favorites: number | null;
  comments: number | null;
  shares: number | null;
  plays: number | null;
  is_top: boolean;
  is_burst: boolean;
  median_likes: number | null;
  detail_fetched: boolean;
  detail_available: boolean;
  extra?: Record<string, unknown>;
  /** 该条笔记是否已下载过（图片原图/动图 + 视频 + 元数据记录）。 */
  downloaded?: boolean;
  downloading?: boolean;
  download?: BenchWorkDownload;
}

export interface BenchStatus {
  state: string;
  message: string;
  ts: number;
  running?: boolean;
  cooldown_left?: number;
}

export interface BenchAccount {
  id: string;
  platform: string;
  platform_name: string;
  account_id: string;
  home_url: string;
  nickname: string;
  avatar: string;
  bio: string;
  remark: string;
  tags: string[];
  followers: BenchMetric | null;
  likes: BenchMetric | null;
  favorites: BenchMetric | null;
  comments: BenchMetric | null;
  posts: number | null;
  coverage: string;
  source: string;
  warnings: string[];
  logged_in?: boolean;
  /** 本次抓取被判游客态（cookie 失效/降级）且已保留上次作品列表时置真。 */
  degraded?: boolean;
  median_likes: number | null;
  work_count: number;
  created: number;
  fetched_at: number;
  last_error: string;
  status: string;
  fetch?: BenchStatus;
  cooldown_left?: number;
  running?: boolean;
}

export interface BenchDetail {
  account: BenchAccount;
  platform: BenchPlatform | Record<string, never>;
  works: BenchWork[];
  coverage: string;
  source: string;
  fetched_at: number;
  median_likes: number | null;
  fetch: BenchStatus;
  cooldown_left: number;
  cooldown: number;
}

/** 单条笔记的下载结果摘要（文件数/体积/记录文件）。 */
export interface BenchWorkDownload {
  note_id: string;
  kind: string;
  files: number;
  bytes: number;
  downloaded_at?: number;
  dir: string;
  note_md: string;
  warnings: string[];
}

/** 下载这条作品的全部数据（元数据 + 图片原图/动图 + 视频，默认 1080p）。 */
export function downloadBenchWork(bid: string, wid: string, quality = '1080'): Promise<{
  state: string; note_id?: string; message?: string; reused?: boolean;
}> {
  return request(`/api/benchmarks/${encodeURIComponent(bid)}/works/${encodeURIComponent(wid)}`
    + `/download?quality=${quality}`, { method: 'POST' });
}

/** 平台 cookie 状态（只回长度/是否含必需项，不回 cookie 值）。 */
export interface BenchCookieStatus {
  platform: string;
  name: string;
  saved: boolean;
  note: string;
  required: string[];
  cookie_count?: number;
  saved_at?: number;
  has_required?: boolean;
  missing?: string[];
}

export function fetchBenchCookies(): Promise<BenchCookieStatus[]> {
  return request('/api/benchmarks/cookies');
}

/** 保存从浏览器手动复制的 cookie（小红书等平台纯 HTTP 取数用）。 */
export function saveBenchCookie(platform: string, cookie: string): Promise<BenchCookieStatus> {
  return request(`/api/benchmarks/cookies/${encodeURIComponent(platform)}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ cookie }),
  });
}

export function clearBenchCookie(platform: string): Promise<BenchCookieStatus> {
  return request(`/api/benchmarks/cookies/${encodeURIComponent(platform)}`, { method: 'DELETE' });
}

/** 平台能力矩阵（哪些平台能自动抓、能拿到哪些字段）。 */
export function fetchBenchPlatforms(): Promise<{ platforms: BenchPlatform[] }> {
  return request('/api/benchmarks/platforms');
}

export function fetchBenchmarks(): Promise<BenchAccount[]> {
  return request('/api/benchmarks');
}

export function fetchBenchmark(id: string): Promise<BenchDetail> {
  return request(`/api/benchmarks/${encodeURIComponent(id)}`);
}

export function createBenchmark(item: {
  platform: string; target: string; remark?: string; tags?: string[];
}): Promise<BenchAccount> {
  return request('/api/benchmarks', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(item),
  });
}

export function deleteBenchmark(id: string): Promise<{ ok: boolean; deleted: string }> {
  return request(`/api/benchmarks/${encodeURIComponent(id)}`, { method: 'DELETE' });
}

/** 触发一次抓取（异步；抓一次 30s～几分钟，随后轮询 status）。
 *  小红书会顺带同步素材：maxDownloads = 本次最多补下几条新作品（0 = 只更新数据不下素材）。 */
export function fetchBenchmarkNow(id: string, maxDownloads?: number): Promise<BenchStatus> {
  const q = maxDownloads === undefined ? '' : `?max_downloads=${maxDownloads}`;
  return request(`/api/benchmarks/${encodeURIComponent(id)}/fetch${q}`, { method: 'POST' });
}

export function benchmarkFetchStatus(id: string): Promise<BenchStatus> {
  return request(`/api/benchmarks/${encodeURIComponent(id)}/fetch/status`);
}

/** 串行刷新全部可自动抓取的账号。 */
export function fetchAllBenchmarks(): Promise<{ state: string; total: number }> {
  return request('/api/benchmarks/fetch-all', { method: 'POST' });
}

export function benchmarkFetchAllStatus(): Promise<{
  running: boolean; done: number; total: number; states: Record<string, string>;
}> {
  return request('/api/benchmarks/fetch-all/status');
}

/** 深挖单条作品（收藏/评论）；平台未提供作品 ID 时后端会明确拒绝。 */
export function fetchBenchmarkWorkDetail(id: string, workId: string): Promise<BenchWork> {
  return request(`/api/benchmarks/${encodeURIComponent(id)}/works/${encodeURIComponent(workId)}/detail`,
    { method: 'POST' });
}

/** 批量补齐收藏/评论：逐条打开作品页读取（串行 + 间隔，命中风控即停）。
 *  limit 省略 = 把待补的全部补完（后端上限 40 条）。 */
export function fetchBenchmarkWorksDetailBatch(id: string, limit = 0): Promise<{
  state: string; total?: number; pending?: number; message?: string; reused?: boolean;
}> {
  return request(`/api/benchmarks/${encodeURIComponent(id)}/works/detail-batch?limit=${limit}`,
    { method: 'POST' });
}

/** 用户显式停止：终止后端正在跑的对话 agent，释放会话锁，令下一句能立刻发。 */
export function stopChat(sessionId: string): Promise<{ stopped: boolean }> {
  return request('/api/chat/stop', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ sessionId }),
  });
}

export function streamChat(
  message: string,
  persona: string | undefined,
  sessionId: string,
  onToken: (chunk: string) => void,
  onDone: (sessionKey?: string) => void,
  onError: (err: Error) => void,
  onThinking?: (chunk: string) => void,
  onActivity?: (status: string) => void,
  onInterrupted?: () => void,
  turnId?: string,
  resumeOnly = false,
  onRecoveryUnavailable?: () => void,
  attachments: UploadedFile[] = [],
  onQuestion?: (q: ChatQuestion) => void,
  onHeartbeat?: (note: string) => void,
): AbortController {
  const controller = new AbortController();
  let lastEventId = 0;

  const consume = async (res: Response): Promise<boolean> => {
      if (!res.ok) {
        let detail = `Stream error: ${res.status}`;
        try {
          const payload = await res.json() as { detail?: string };
          if (payload.detail) detail = payload.detail;
        } catch { /* non-JSON error */ }
        const error = new Error(detail) as Error & { status?: number };
        error.status = res.status;
        throw error;
      }
      const reader = res.body?.getReader();
      if (!reader) throw new Error('No response body');

      const decoder = new TextDecoder();
      let buffer = '';
      let currentEvent = '';
      let currentId = 0;
      let dataLines: string[] = [];

      const dispatch = () => {
        if (!currentEvent || dataLines.length === 0) return false;
        if (currentId > lastEventId) lastEventId = currentId;
        const data = dataLines.join('\n');
        if (currentEvent === 'token') {
          try { onToken(JSON.parse(data) as string); } catch { onToken(data); }
        } else if (currentEvent === 'thinking' && onThinking) {
          try { onThinking(JSON.parse(data) as string); } catch { onThinking(data); }
        } else if (currentEvent === 'activity' && onActivity) {
          try { onActivity(JSON.parse(data) as string); } catch { onActivity(data); }
        } else if (currentEvent === 'question' && onQuestion) {
          // 防御：question 事件负载必须是对象且带 questions 数组。旧后端重放流曾把
          // question 双层序列化（JSON.parse 后得到字符串），透传会渲染出无法作答的
          // 空壳卡片——宁可不显示，交给用户手动追问。
          try {
            const q = JSON.parse(data) as ChatQuestion;
            if (q && typeof q === 'object' && Array.isArray(q.questions)) onQuestion(q);
          } catch { /* 解析失败忽略 */ }
        } else if (currentEvent === 'heartbeat') {
          // 防呆心跳：独立于 activity/thinking，仅作「未卡住」提示，不覆盖真实状态。
          if (onHeartbeat) { try { onHeartbeat(JSON.parse(data) as string); } catch { onHeartbeat(data); } }
        } else if (currentEvent === 'error') {
          let msg = '执行失败';
          try { msg = JSON.parse(data) as string; } catch { msg = data; }
          onError(new Error(msg));
          return true;
        } else if (currentEvent === 'done') {
          try { onDone((JSON.parse(data) as { sessionKey?: string }).sessionKey); } catch { onDone(); }
          return true;
        }
        return false;
      };

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split(/\r?\n/);
        buffer = lines.pop() || '';

        for (const line of lines) {
          if (line === '') {
            if (dispatch()) return true;
            currentEvent = ''; currentId = 0; dataLines = [];
          } else if (line.startsWith('id:')) currentId = Number(line.slice(3).trim()) || 0;
          else if (line.startsWith('event:')) currentEvent = line.slice(6).trim();
          else if (line.startsWith('data:')) dataLines.push(line.slice(5).replace(/^ /, ''));
        }
      }
      buffer += decoder.decode();
      if (buffer) {
        for (const line of buffer.split(/\r?\n/)) {
          if (line.startsWith('id:')) currentId = Number(line.slice(3).trim()) || 0;
          else if (line.startsWith('event:')) currentEvent = line.slice(6).trim();
          else if (line.startsWith('data:')) dataLines.push(line.slice(5).replace(/^ /, ''));
        }
      }
      return dispatch();
  };

  void (async () => {
    const started = Date.now();
    let first = !resumeOnly;
    while (!controller.signal.aborted) {
      try {
        const res = first
          ? await fetch(`${BASE}/api/chat/stream`, {
              method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ message, persona: persona || undefined, sessionId, turnId, attachments }),
              signal: controller.signal,
            })
          : await fetch(`${BASE}/api/chat/jobs/${encodeURIComponent(turnId || '')}/stream?after=${lastEventId}`, {
              signal: controller.signal,
            });
        if (!first && res.status === 404 && onRecoveryUnavailable) {
          onRecoveryUnavailable();
          return;
        }
        if (await consume(res)) return;
      } catch (err: unknown) {
        if (err instanceof Error && err.name === 'AbortError') return;
        const status = (err as Error & { status?: number })?.status;
        if (first && status && status >= 400 && status < 500) {
          onError(err instanceof Error ? err : new Error('请求失败'));
          return;
        }
      }
      first = false;
      // Backend chat runs may spend 5 minutes waiting for a session lock and then
      // run for 2 hours. Keep reconnecting for the same end-to-end budget.
      if (!turnId || Date.now() - started >= 130 * 60 * 1000) {
        onError(new Error('连接中断，自动重连超时'));
        return;
      }
      onInterrupted?.();
      await new Promise((resolve) => setTimeout(resolve, 1000));
    }
  })();

  return controller;
}

/** 取某会话最近一轮的完整结果（SSE 断线后据此取回）。 */
export function fetchLastTurn(sessionId: string, turnId?: string): Promise<{ status: string; text: string; turn_id?: string }> {
  const query = turnId ? `?turn_id=${encodeURIComponent(turnId)}` : '';
  return request(`/api/chat/last/${encodeURIComponent(sessionId)}${query}`);
}

// ═══ 设置面板 · 环境安装（install_tool 引擎桥） ═══

export interface EnvTool {
  id: string;
  state: 'ok' | 'missing' | 'no_dir' | 'fail';
  version: string | null;
  detail: string | null;
  name: string;
  group: string;
  group_name?: string;
  desc: string;
  big?: boolean;
}

export interface EnvToolsResponse {
  python: string;
  tools: EnvTool[];
  cachedAt?: number;
}

export function fetchEnvTools(refresh = false): Promise<EnvToolsResponse> {
  return request(`/api/env/tools${refresh ? '?refresh=1' : ''}`);
}

export interface EnvJobResult {
  id: string;
  state: 'ok' | 'fail';
  version?: string | null;
  strategy?: string | null;
  detail?: string | null;
}

export interface EnvJob {
  jobId: string;
  id: string;
  state: 'running' | 'ok' | 'fail';
  lines: string[];
  result: EnvJobResult | null;
  started: number;
  ended: number | null;
}

export function startEnvInstall(id: string): Promise<{ jobId: string; id: string; state: string }> {
  return request('/api/env/install', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id }),
  });
}

export function fetchEnvJob(jobId: string): Promise<EnvJob> {
  return request(`/api/env/job/${encodeURIComponent(jobId)}`);
}

// ═══ 设置面板 · 模型通道（只读 + 真自测） ═══

export interface ModelRow {
  slot?: string;
  order: number;
  name: string;
  sub: string;
  type: string;
  model: string;
  baseUrl: string;
  keyMasked: string;
  role: string;
  result: string;
  protocol?: string;
  thinking?: boolean;
  thinkingFormat?: string;
  keyNew?: string;
  keyNew2?: string;
  key2Label?: string;
  key2Masked?: string;
  modelEditable?: boolean;
  baseEditable?: boolean;
  baseOptional?: boolean;
  adv?: boolean;
  deletable?: boolean;
}

export interface ModelSaveRow {
  slot: string;
  name?: string;
  model: string;
  baseUrl: string;
  key: string;
  key2?: string;
  primary?: boolean;
  /** 自定义供应商上游协议：openai（默认）| anthropic */
  protocol?: string;
  thinking?: boolean;
  thinkingFormat?: string;
}

export interface ModelSaveResponse extends ModelChannelsResponse {
  ok: boolean;
  note?: string;
}

export function saveModelConfig(channel: string, rows: ModelSaveRow[]): Promise<ModelSaveResponse> {
  return request('/api/settings/models/save', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ channel, rows }),
  });
}

export interface ModelChannelRows { rows: ModelRow[] }

export interface ModelChannelsResponse {
  transport?: 'api' | 'openclaw';
  channels: {
    chat: ModelChannelRows;
    transcribe: ModelChannelRows;
    image?: ModelChannelRows;
    video?: ModelChannelRows;
    music?: ModelChannelRows;
    speech?: ModelChannelRows;
  };
  primary: string;
}

export function fetchModelChannels(runtime?: string): Promise<ModelChannelsResponse> {
  return request(`/api/settings/models${runtime ? `?runtime=${encodeURIComponent(runtime)}` : ''}`);
}

export function saveChatTransport(transport: 'api' | 'openclaw'): Promise<ModelChannelsResponse> {
  return request('/api/settings/chat/transport', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ transport }),
  });
}

// （保存接口见上方 saveModelConfig）

export interface SelftestResult { baseUrl: string; ok: boolean; ms: number; detail?: string }

export function runChannelSelftest(channel: string): Promise<{ channel: string; results: SelftestResult[]; testedAt: number }> {
  return request('/api/settings/models/selftest', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ channel }),
  });
}

export interface LocalAgentModel {
  id: string;
  name: string;
  contextWindow?: number | null;
}

export interface LocalAgentInfo {
  id: string;
  label: string;
  installed: boolean;
  command: string;
  path: string;
  openclawProvider: string | null;
  configProvider?: string | null;
  supported: boolean;
  configured: boolean;
  usableWithoutKey: boolean;
  loginHint: string;
  models?: LocalAgentModel[];
}

export function fetchLocalAgents(): Promise<{
  agents: LocalAgentInfo[];
  installedCount: number;
  usableWithoutKeyCount: number;
  usableWithoutKey: string[];
}> {
  return request('/api/settings/local-agents');
}

export function enableLocalAgent(id: string, model = ''): Promise<{ ok: boolean; note: string; agent: LocalAgentInfo }> {
  return request('/api/settings/local-agents/enable', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id, model }),
  });
}

export interface ModelsFetchResponse { baseUrl: string; models: string[]; fetchedAt: number }

/**
 * 拉取某供应商的模型列表（自定义供应商免手打模型名）。
 * key 留空时后端按 slot 从已存配置里取（用户不用为了拉列表重贴一遍 Key）。
 */
export function fetchAvailableModels(
  baseUrl: string, key: string, protocol: string, slot = '',
  name = '',
): Promise<ModelsFetchResponse> {
  return request('/api/settings/models/available', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ baseUrl, key, protocol, slot, name }),
  });
}
