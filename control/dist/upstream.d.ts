import { LlmAdapter } from '@deepseek-ai/dsh-llm';
import type { GenerateOptions } from '@deepseek-ai/dsh-llm';
import { buildResponsesBody } from './upstream_responses.js';
/**
 * Production upstream for the eval broker: an OpenAI-compatible
 * chat-completions {@link LlmAdapter} that the trusted host starts through
 * `startHostBroker`. Credentials arrive by environment-variable name only; the
 * key value never enters configuration, log lines, or error messages.
 *
 * The wire the provider endpoint speaks is the `protocol` option:
 * `chat_completions` — the default, so an
 * existing spec without the key keeps byte-identical behavior — or
 * `responses` (OpenAI Responses API, e.g. DeepSeek's `https://api.deepseek.com`
 * base). Both adapters serve the same neutral
 * {@link GenerateOptions} → {@link StreamChunk} contract, so everything
 * downstream of the adapter (metering, budgets, token bounds) is
 * protocol-agnostic.
 */
/**
 * The upstream wire protocols a provider route can speak.
 */
export type UpstreamProtocol = 'chat_completions' | 'responses';
export interface UpstreamAdapterOptions {
    readonly provider: string;
    readonly baseUrl: string;
    /** Environment variable holding the API key; only the name is configured. */
    readonly apiKeyEnv: string;
    readonly model: string;
    /**
     * Wire protocol of the provider endpoint; defaults to `chat_completions`.
     * The endpoint path is derived from it (`/chat/completions` vs `/responses`).
     */
    readonly protocol?: UpstreamProtocol;
    readonly timeoutMs?: number;
    /** Extra request headers; the adapter's own auth, content-type, and attribution always win. */
    readonly headers?: Record<string, string>;
    /**
     * Reasoning efforts the gateway declares. Neither wire has capability
     * discovery, so the owner states them; a lease pinning an effort that is
     * not declared here refuses to start.
     */
    readonly reasoningEfforts?: readonly string[];
    /**
     * Provider-owned context capacity (combined request + response tokens) for
     * the pinned route. Neither wire offers model discovery, so the owner states
     * it; ``resolveModel`` echoes it as ``context.contextWindow`` so the harness
     * records it in the sealed ``request/context`` session event and downstream
     * evidence (the ATIF agent block) self-carries the window instead of relying
     * on a caller-declared flag. Absent leaves the capacity unadvertised — never
     * guessed — and occupancy stays uncomputable downstream.
     */
    readonly contextWindow?: number;
}
/** One OpenAI chat message exactly as it appears on the wire. */
export type WireMessage = {
    readonly role: 'system' | 'developer' | 'user';
    readonly content: string;
} | {
    readonly role: 'assistant';
    readonly content?: string;
    readonly tool_calls?: readonly WireToolCall[];
} | {
    readonly role: 'tool';
    readonly tool_call_id: string;
    readonly content: string;
};
export interface WireToolCall {
    readonly id: string;
    readonly type: 'function';
    readonly function: {
        readonly name: string;
        readonly arguments: string;
    };
}
/** The exact JSON body POSTed to `/chat/completions` for one request. */
export interface ChatCompletionsBody {
    readonly model: string;
    readonly messages: readonly WireMessage[];
    readonly stream: true;
    readonly max_tokens?: number;
    readonly temperature?: number;
    readonly stop?: readonly string[];
    readonly tools?: readonly {
        readonly type: 'function';
        readonly function: {
            readonly name: string;
            readonly description: string;
            readonly parameters: Record<string, unknown>;
        };
    }[];
}
/** Read and validate the API key; the value itself never enters the message. */
export declare function readUpstreamKey(apiKeyEnv: string): string;
/**
 * Serialize one provider-neutral request to the exact chat-completions wire
 * body. The meter must count this same body, so the mapping lives here once.
 */
export declare function buildChatCompletionsBody(model: string, options: Readonly<GenerateOptions>): ChatCompletionsBody;
/** Build the exact request body a dispatch over `protocol` would send. */
export declare function buildUpstreamRequestBody(protocol: UpstreamProtocol, model: string, options: Readonly<GenerateOptions>): ChatCompletionsBody | ReturnType<typeof buildResponsesBody>;
/** Build the production upstream adapter for one provider route. */
export declare function createUpstreamAdapter(options: UpstreamAdapterOptions): LlmAdapter;
