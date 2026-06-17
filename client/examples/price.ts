/**
 * Example: fetch an Ozon price and run a custom scenario.
 * Run with:  npm run example:price -- "https://www.ozon.ru/product/..."
 */
import { createScrapeClient } from "../src/index.js";

const baseUrl = process.env.SCRAPE_URL ?? "http://127.0.0.1:8077";
const url =
  process.argv[2] ??
  "https://www.ozon.ru/product/akvashuz-korally-pesok-galka-otpusk-2025-4382692671/";

const client = createScrapeClient(baseUrl);

async function main() {
  const health = await client.health();
  console.log("health:", health);

  // 1) Convenience endpoint
  const price = await client.ozonPrice({ url, use_cache: true });
  console.log("ozonPrice:", {
    ok: price.ok,
    price_value: price.price_value,
    card_price_value: price.card_price_value,
    price_text: price.price_text,
  });

  // 2) Generic command scenario — fully typed
  const run = await client.run({
    start_url: url,
    steps: [
      { action: "wait_for", selector: "[data-widget=webPrice]", timeout_s: 15 },
      { action: "extract", name: "title", selector: "h1", kind: "text" },
      {
        action: "extract",
        name: "price",
        selector: "[data-widget=webPrice]",
        kind: "text",
      },
    ],
  });
  console.log("run.ok:", run.ok, "data:", run.data);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
