import { LlmAdapter } from '@deepseek-ai/dsh-llm';
import type { GenerateOptions, LlmProviderInfo, LlmResolvedModelInfo, StreamChunk } from '@deepseek-ai/dsh-llm';
/**
 * The Responses-API production upstream.
 *
 * Some model APIs are served in OpenAI's Responses format rather than chat
 * completions — DeepSeek's is the deployed example: base_url
 * `https://api.deepseek.com`, endpoint `POST /responses` (api-docs.deepseek.com,
 * "Using the Responses API": the endpoint exists "to meet the demand for
 * Codex"). This adapter speaks that wire against the same neutral
 * {@link GenerateOptions} → {@link StreamChunk} contract the chat-completions
 * adapter serves, so the broker's metering, budget, and token-bound semantics
 * are identical in both modes.
 *
 * Wire facts this file encodes (all from the DeepSeek Responses API docs,
 * 2026-09-30):
 *
 * - requests carry `model`, `input` (a string or a list of items:
 *   `message` / `function_call` / `function_call_output` / `reasoning`),
 *   `instructions`, `reasoning.effort`, `max_output_tokens`, `stream`,
 *   `temperature`, `tools` (function only), `tool_choice`, `text.format`;
 *   there is **no `stop` parameter** — an option the wire cannot represent is
 *   refused here rather than silently dropped (dropping it would change what
 *   the meter counts).
 * - the API is stateless: no `previous_response_id`; each turn re-sends the
 *   full history, exactly like the chat wire.
 * - streaming is semantic SSE (`response.created`, `response.output_item.added`,
 *   `response.output_text.delta`, `response.reasoning_text.delta`,
 *   `response.function_call_arguments.delta`, `response.output_item.done`,
 *   …) and the stream ENDS with a terminal `response.completed` /
 *   `response.incomplete` / `response.failed` event — there is no
 *   `data: [DONE]` sentinel.
 * - usage is `input_tokens` (+`input_tokens_details.cached_tokens`),
 *   `output_tokens` (+`output_tokens_details.reasoning_tokens`),
 *   `total_tokens`.
 */
/** One input item exactly as it appears on the `/responses` wire. */
export type ResponsesInputItem = {
    readonly type: 'message';
    readonly role: 'user' | 'assistant' | 'system' | 'developer';
    readonly content: string;
} | {
    readonly type: 'function_call';
    readonly call_id: string;
    readonly name: string;
    readonly arguments: string;
} | {
    readonly type: 'function_call_output';
    readonly call_id: string;
    readonly output: string;
};
/** The exact JSON body POSTed to `/responses` for one request. */
export interface ResponsesBody {
    readonly model: string;
    readonly input: readonly ResponsesInputItem[];
    readonly stream: true;
    readonly instructions?: string;
    readonly max_output_tokens?: number;
    readonly temperature?: number;
    readonly tools?: readonly {
        readonly type: 'function';
        readonly name: string;
        readonly description: string;
        readonly parameters: Record<string, unknown>;
    }[];
}
/**
 * Serialize one provider-neutral request to the exact responses wire body.
 * The meter must count this same body, so the mapping lives here once.
 */
export declare function buildResponsesBody(model: string, options: Readonly<GenerateOptions>): ResponsesBody;
/** The `/responses` counterpart of the chat-completions upstream adapter. */
export declare class ResponsesAdapter extends LlmAdapter {
    #private;
    constructor(model: string, url: string, headers: Record<string, string>, key: string, timeoutMs: number | undefined, efforts: readonly string[], contextWindow: number | undefined);
    providerInfo(provider: string): LlmProviderInfo;
    resolveModel(provider: string, model: string): Promise<LlmResolvedModelInfo>;
    stream(options: GenerateOptions): AsyncIterable<StreamChunk>;
}
