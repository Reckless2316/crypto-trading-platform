// Command sentinel is SURGE Sentinel: a real-time, self-calibrating crypto
// indicator engine with a reasoning agent on top.
//
//	sentinel analyze  -symbol BTC-USDT -tf 1h          one-shot analysis (JSON or text)
//	sentinel scan     -symbols BTC-USDT,ETH-USDT,...  rank a universe
//	sentinel backtest -symbol BTC-USDT -tf 1h -bars 8000
//	sentinel serve    -symbols ... -addr 127.0.0.1:8090   live engine + API + dashboard
//	sentinel agent    -symbol BTC-USDT "question"      Claude analyst over Sentinel tools
//	sentinel mcp                                       MCP server on stdio
//	sentinel fetch    -symbol BTC-USDT -tf 1h -bars 5000 -out bars.csv
//	sentinel verify   -ledger data/sentinel-ledger.jsonl
package main

import (
	"fmt"
	"os"
)

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	cmd, args := os.Args[1], os.Args[2:]
	var err error
	switch cmd {
	case "analyze":
		err = cmdAnalyze(args)
	case "scan":
		err = cmdScan(args)
	case "backtest":
		err = cmdBacktest(args)
	case "serve":
		err = cmdServe(args)
	case "agent":
		err = cmdAgent(args)
	case "mcp":
		err = cmdMCP(args)
	case "fetch":
		err = cmdFetch(args)
	case "verify":
		err = cmdVerify(args)
	case "-h", "--help", "help":
		usage()
		return
	default:
		fmt.Fprintf(os.Stderr, "unknown command %q\n\n", cmd)
		usage()
		os.Exit(2)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}
}

func usage() {
	fmt.Fprint(os.Stderr, `SURGE Sentinel: real-time, self-calibrating crypto indicators with a reasoning agent.

Commands:
  analyze    one-shot multi-timeframe analysis of a symbol
  scan       analyse and rank a universe of symbols
  backtest   walk-forward (prequential) evaluation with costs
  serve      live engine: OKX websocket, HTTP API, SSE stream, dashboard
  agent      ask the Claude analyst agent (needs ANTHROPIC_API_KEY)
  mcp        Model Context Protocol server on stdio (for Claude Desktop, Hermes, etc.)
  fetch      download OKX candles to CSV
  verify     verify a signal ledger's hash chain

Run "sentinel <command> -h" for flags. Market data is public; no exchange keys are read
and no orders are ever placed.
`)
}
