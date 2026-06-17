/**
 * Typesafe client for the nodriver scraping service.
 *
 * The heavy lifting (zod schemas + endpoint wiring) lives in the generated
 * `generated.ts`, which is produced from the service's OpenAPI document via
 * `npm run generate`. This file adds friendly method names and exported types
 * so consumers never touch the auto-generated alias names.
 */
import { z } from "zod";
import { createApiClient, schemas } from "./generated.js";

export { schemas } from "./generated.js";

// ---- Inferred types (use these in your Node.js code) --------------------- //
export type Proxy = z.infer<typeof schemas.Proxy>;
export type Step = z.infer<
  | typeof schemas.NavigateStep
  | typeof schemas.WaitForStep
  | typeof schemas.WaitForTextStep
  | typeof schemas.SleepStep
  | typeof schemas.ClickStep
  | typeof schemas.ScrollStep
  | typeof schemas.ExtractStep
  | typeof schemas.FindTextStep
  | typeof schemas.EvalStep
  | typeof schemas.ScreenshotStep
>;
export type RunRequest = z.infer<typeof schemas.RunRequest>;
export type RunResponse = z.infer<typeof schemas.RunResponse>;
export type OzonPriceRequest = z.infer<typeof schemas.OzonPriceRequest>;
export type OzonPriceResponse = z.infer<typeof schemas.OzonPriceResponse>;
export type HealthResponse = z.infer<typeof schemas.HealthResponse>;

/**
 * Create a typesafe client.
 *
 * @example
 * const client = createScrapeClient("http://127.0.0.1:8077");
 * const { price_value } = await client.ozonPrice({ url });
 */
export function createScrapeClient(
  baseUrl: string,
  options?: { token?: string; timeoutMs?: number },
) {
  const api = createApiClient(baseUrl, {
    axiosConfig: {
      timeout: options?.timeoutMs ?? 120_000,
      headers: options?.token
        ? { Authorization: `Bearer ${options.token}` }
        : undefined,
    },
  });

  return {
    /** Raw zodios client (all auto-generated aliases). */
    raw: api,
    health(): Promise<HealthResponse> {
      return api.health_health_get();
    },
    /** Open an Ozon product page and return its parsed price. */
    ozonPrice(body: OzonPriceRequest): Promise<OzonPriceResponse> {
      return api.ozon_price_ozon_price_post(body);
    },
    /** Run an arbitrary command scenario and return extracted data. */
    run(body: RunRequest): Promise<RunResponse> {
      return api.run_run_post(body);
    },
  };
}

export type ScrapeClient = ReturnType<typeof createScrapeClient>;
