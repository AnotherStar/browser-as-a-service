# nodriver-scrape-client

Типобезопасный (zod) клиент для сервиса скрейпинга, сгенерированный из его
OpenAPI-документа. Подробности и архитектура — в [корневом README](../README.md).

```bash
npm install
npm run generate   # openapi.json -> src/generated.ts (+ patch на z.literal)
npm run build      # сборка в dist/
npm run example:price
```

- `src/generated.ts` — **генерируется**, не редактировать руками.
- `src/index.ts` — удобная обёртка (`createScrapeClient`) + выведенные типы.
- `patch-generated.mjs` — чинит `const`-дискриминаторы на `z.literal`
  (ограничение openapi-zod-client), чтобы работал `z.discriminatedUnion`.
