import { attributionHeaders, LlmError } from '@deepseek-ai/dsh-llm';
import type { GenerateOptions } from '@deepseek-ai/dsh-llm';
import { buildChatCompletionsBody, readUpstreamKey } from './upstream.js';

/**
 * Trusted input-token metering for hard token budgets.
 *
 * A hard `limits.maxTokens` budget is only as trustworthy as its input bound:
 * the broker must refuse the request when the provider-side count of the exact
 * dispatch cannot be obtained. The official dsh-token-meter
 * `CHARS_PER_TOKEN=4` heuristic is NOT a hard upper bound and must never be
 * wired in here — heuristic estimation cannot back a hard budget, so this
 * module provides no heuristic path at all. Without a trusted counting source
 * configured, `startHostBroker` keeps refusing hard budgets outright.
 */

export interface ProviderCountBoundOptions {
  readonly baseUrl: string;
  readonly apiKeyEnv: string;
  /** Absolute URL of the counting endpoint; defaults to `${baseUrl}/tokens/count`. */
  readonly endpoint?: string;
  /** Tokens added to the counted value to absorb framing drift; defaults to 8. */
  readonly margin?: number;
  readonly timeoutMs?: number;
}

/**
 * Bound one request's provider input tokens. Returns the counted value plus
 * the margin; any failure (transport, timeout, non-2xx, malformed payload)
 * throws so the broker refuses the hard-budget request instead of dispatching
 * on an unmeasured prompt.
 */
export type ProviderInputTokenBound = (request: Readonly<GenerateOptions>) => Promise<number>;

const DEFAULT_TIMEOUT_MS = 30_000;
const DEFAULT_MARGIN = 8;
const TIMER_RANGE_MS = 2_147_483_647;
// A count response is a handful of numbers; anything larger is not a count.
const MAX_COUNT_RESPONSE_BYTES = 64 * 1024;

function invalid(message: string): never {
  throw new LlmError(`token count bound: ${message}`, 'INVALID_CONFIG');
}

function countEndpoint(options: ProviderCountBoundOptions): string {
  const endpoint = options.endpoint ?? `${options.baseUrl.replace(/\/+$/u, '')}/tokens/count`;
  let url: URL;
  try {
    url = new URL(endpoint);
  } catch {
    return invalid('endpoint is not a valid URL');
  }
  const loopback = url.hostname === 'localhost' || url.hostname === '[::1]' || /^127(?:\.\d{1,3}){3}$/u.test(url.hostname);
  if (url.protocol !== 'https:' && !(url.protocol === 'http:' && loopback)) return invalid('endpoint requires HTTPS except for loopback HTTP');
  if (url.username || url.password || url.hash) return invalid('endpoint must not carry userinfo or a fragment');
  return endpoint;
}

function counted(raw: unknown): number {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    throw new LlmError('token count endpoint returned a malformed payload', 'MALFORMED_RESPONSE');
  }
  const value = raw as Record<string, unknown>;
  for (const key of ['inputTokens', 'total_tokens']) {
    const count = value[key];
    if (count === undefined) continue;
    if (typeof count !== 'number' || !Number.isSafeInteger(count) || count < 0) {
      throw new LlmError(`token count endpoint returned an invalid ${key}`, 'MALFORMED_RESPONSE');
    }
    return count;
  }
  throw new LlmError('token count endpoint returned neither inputTokens nor total_tokens', 'MALFORMED_RESPONSE');
}

async function boundedText(response: Response): Promise<string> {
  if (!response.body) throw new LlmError('token count endpoint returned no body', 'MALFORMED_RESPONSE');
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) return new TextDecoder('utf-8', { fatal: true }).decode(Buffer.concat(chunks));
      size += value.byteLength;
      if (size > MAX_COUNT_RESPONSE_BYTES) throw new LlmError('token count response exceeds its bound', 'MALFORMED_RESPONSE');
      chunks.push(value);
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}

/** Build a provider-backed input-token bound for one upstream route. */
export function createProviderCountBound(options: ProviderCountBoundOptions): ProviderInputTokenBound {
  if (typeof options.baseUrl !== 'string') invalid('baseUrl must be a string');
  const endpoint = countEndpoint(options);
  const margin = options.margin === undefined ? DEFAULT_MARGIN : options.margin;
  if (!Number.isSafeInteger(margin) || margin < 0) invalid('margin must be a non-negative safe integer');
  const timeoutMs = options.timeoutMs === undefined ? DEFAULT_TIMEOUT_MS : options.timeoutMs;
  if (!Number.isSafeInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > TIMER_RANGE_MS) invalid('timeoutMs must be a positive safe integer within timer range');
  const headers = Object.freeze({
    ...attributionHeaders(),
    'content-type': 'application/json',
    authorization: `Bearer ${readUpstreamKey(options.apiKeyEnv)}`,
  });
  return Object.freeze(async (request: Readonly<GenerateOptions>): Promise<number> => {
    // The deadline is per call: one shared timer would expire the meter for
    // every later request once the broker has been up for timeoutMs.
    const timeout = AbortSignal.timeout(timeoutMs);
    const signal = request.signal === undefined ? timeout : AbortSignal.any([request.signal, timeout]);
    // Count the exact wire body the dispatch adapter will send, so clamping
    // that changes the request is reflected in what gets counted.
    const body = JSON.stringify(buildChatCompletionsBody(request.model, request));
    let response: Response;
    try {
      response = await fetch(endpoint, { method: 'POST', headers, body, redirect: 'error', signal });
    } catch (error) {
      if (request.signal?.aborted) throw error;
      if (timeout.aborted) throw new LlmError(`token count endpoint timed out after ${timeoutMs}ms`, 'TIMEOUT');
      throw new LlmError(`token count endpoint failed: ${error instanceof Error ? error.message : String(error)}`, 'TRANSPORT', { cause: error });
    }
    try {
      if (!response.ok) throw new LlmError(`token count endpoint answered HTTP ${response.status}`, response.status >= 500 ? 'SERVER' : 'INVALID_REQUEST', { status: response.status });
      const text = await boundedText(response);
      let raw: unknown;
      try {
        raw = JSON.parse(text);
      } catch {
        throw new LlmError('token count endpoint returned invalid JSON', 'MALFORMED_RESPONSE');
      }
      const value = counted(raw);
      if (value > Number.MAX_SAFE_INTEGER - margin) throw new LlmError('token count exceeds the safe integer range', 'MALFORMED_RESPONSE');
      return value + margin;
    } finally {
      await response.body?.cancel().catch(() => {});
    }
  });
}
