// 统一的 API 请求封装:
// - 所有请求 120 秒超时(AbortController);
// - 非 2xx 响应统一转成中文 ApiError(detail/reason 优先,兼容 FastAPI 校验错误数组);
// - 网络不通 / 超时给出中文场景文案,由各面板以错误条 + toast 展示。

// 需求约定:请求超时 120s
const DEFAULT_TIMEOUT = 120000;

/** 携带 HTTP 状态码与后端原始 JSON 的错误对象,便于调用方处理 422 的 verdict 等数据。 */
export class ApiError extends Error {
  constructor(message, status, data) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.data = data;
  }
}

// 把 fetch 抛出的原生异常(AbortError / TypeError)翻译成中文文案
function normalizeNativeError(err) {
  if (err && err.name === 'AbortError') {
    return new ApiError(
      '请求超时:后端可能仍在计算(真实 LLM 对话 / 多策略竞技场可能需要数分钟),请稍后重试。',
      0,
      null,
    );
  }
  // fetch 连接失败通常是 TypeError: Failed to fetch
  return new ApiError(
    '无法连接后端服务:请确认 FastAPI 已启动(默认 http://localhost:8000),且开发代理已配置 /api。',
    0,
    null,
  );
}

// 从后端错误 body 里提取可读信息:优先 detail / reason;数组按 FastAPI 校验错误格式展开
function extractDetail(data) {
  if (!data) return '';
  const d = data.detail ?? data.reason;
  if (typeof d === 'string' && d) return d;
  if (Array.isArray(d)) {
    return d
      .map((it) => `${(it.loc || []).join('.')}: ${it.msg}`)
      .join('; ');
  }
  if (d) return JSON.stringify(d);
  return '';
}

// 底层请求:返回解析后的 JSON;任何失败都抛 ApiError(中文消息)
async function request(path, { method = 'GET', body, timeout = DEFAULT_TIMEOUT } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  let res;
  let raw = '';
  try {
    res = await fetch(path, {
      method,
      headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: controller.signal,
    });
    // 先读文本再 JSON.parse,读体阶段的中断也能被统一捕获
    raw = await res.text();
  } catch (err) {
    throw normalizeNativeError(err);
  } finally {
    clearTimeout(timer);
  }

  // JSON 解析容错:后端异常时可能返回纯文本
  let data = null;
  if (raw) {
    try {
      data = JSON.parse(raw);
    } catch {
      data = null;
    }
  }

  if (!res.ok) {
    const extra = extractDetail(data);
    let msg;
    if (extra) {
      msg = `后端返回 HTTP ${res.status}:${extra}`;
    } else if (res.status >= 500) {
      // 无 JSON body 的 5xx:多为服务未启动 / 代理不可达(vite 代理转发失败)
      msg = `后端服务异常(HTTP ${res.status}):请确认 FastAPI 已启动且 /api 代理指向正确端口。`;
    } else {
      msg = `后端返回 HTTP ${res.status},接口异常。`;
    }
    throw new ApiError(msg, res.status, data);
  }
  return data;
}

// 平台四个端点(契约见 docs/design.md 第 3 节)
export const api = {
  // GET /api/health → {"status":"ok","version"}(健康检查用短超时,快速判定后端在线)
  health: () => request('/api/health', { method: 'GET', timeout: 8000 }),
  // POST /api/chat {text,start,end} → 编排管线结果(steps/code/verdict/metrics/report)
  chat: (payload) => request('/api/chat', { method: 'POST', body: payload, timeout: 300000 }),  // 真实 LLM+自愈循环耗时更长
  // POST /api/validate {code,start?,end?} → Layer 1 校验结论 + 中文摘要
  validate: (payload) => request('/api/validate', { method: 'POST', body: payload }),
  // POST /api/backtest {code,start,end,cash?,overrides?} → 200 指标 / 422 拦截(verdict)
  backtest: (payload) => request('/api/backtest', { method: 'POST', body: payload }),
  // POST /api/verify {code,start,end,is_ratio?,cash?,benchmark?,overrides?} →
  // 分层验证(Layer 4,样本内/样本外对照 + checks + 随机基准 + 模拟盘清单)。
  // 内部要跑两次回测(约 5~20 秒),超时放宽到 300s。
  verify: (payload) => request('/api/verify', { method: 'POST', body: payload, timeout: 300000 }),
  // POST /api/arena {candidates(1~6),start,end,cash?} → 多策略竞技场排名
  // (候选数 × 回测耗时,真实 bundle 回测较慢,超时放宽到 600s)
  arena: (payload) => request('/api/arena', { method: 'POST', body: payload, timeout: 600000 }),
  // POST /api/optimize {code,start,end,rounds?(1~5,默认2),cash?,overrides?} →
  // AI 自动优化:先跑基线回测,再循环"LLM 分析 → 改写 → 回测对照"N 轮,返回最优版本与演进史。
  // 每轮 ≈ 1 次 LLM 生成(30~120s)+ 2 次回测;rounds=2 常见 2~5 分钟 → 超时放宽到 900s。
  optimize: (payload) => request('/api/optimize', { method: 'POST', body: payload, timeout: 900000 }),
  // GET /api/kline?oid=&start=&end= → K 线行情(真实价日线,最多 500 根,超出后端抽样)
  // GET + query 参数;oid 必填,start/end('YYYY-MM-DD')可选,缺省 120s 超时
  kline: ({ oid, start, end } = {}) => {
    const qs = new URLSearchParams({ oid: String(oid ?? '') });
    if (start) qs.set('start', start);
    if (end) qs.set('end', end);
    return request(`/api/kline?${qs.toString()}`, { method: 'GET', timeout: DEFAULT_TIMEOUT });
  },
};
