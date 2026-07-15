const port = Number.parseInt(process.env.ADAPTER_PORT ?? "8082", 10);
const response = await fetch(`http://127.0.0.1:${port}/health`, {
  signal: AbortSignal.timeout(2000),
});

if (!response.ok) {
  process.exitCode = 1;
}
