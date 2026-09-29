import { LlmAdapter } from '@deepseek-ai/dsh-llm';
import type { GenerateOptions } from '@deepseek-ai/dsh-llm';
/**
 * Production upstream for the eval broker: an OpenAI-compatible
 * chat-completions {@link LlmAdapter} that the trusted host starts through
 * `startHostBroker`. Credentials arrive by environment-variable name only; the
 * key value never enters configuration, log lines, or error messages.
 */
export interface UpstreamAdapterOptions {
    readonly provider: string;
    readonly baseUrl: string;
    /** Environment variable holding the API key; only the name is configured. */
    readonly apiKeyEnv: string;
    readonly model: string;
    readonly timeoutMs?: number;
    /** Extra request headers; the adapter's own auth, content-type, and attribution always win. */
    readonly headers?: Record<string, string>;
    /**
     * Reasoning efforts the gateway declares. The chat-completions wire has no
     * capability discovery, so the owner states them; a lease pinning an effort
     * that is not declared here refuses to start.
     */
    readonly reasoningEfforts?: readonly string[];
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
/** Build the production chat-completions upstream adapter for one provider route. */
export declare function createUpstreamAdapter(options: UpstreamAdapterOptions): LlmAdapter;
