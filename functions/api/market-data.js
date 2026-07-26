export async function onRequestGet({ env }) {
  if (!env.MARKET_DATA) {
    return Response.json({ error: 'MARKET_DATA KV binding is not configured' }, { status: 503 });
  }
  let marketData;
  try {
    marketData = await env.MARKET_DATA.get('market-data', { type: 'json' });
  } catch {
    return Response.json({ error: 'Market data could not be read' }, { status: 502 });
  }
  if (marketData === null) {
    return Response.json({ error: 'Market data is not available' }, { status: 404 });
  }
  return Response.json(marketData, {
    headers: { 'Cache-Control': 'public, max-age=300' }
  });
}
