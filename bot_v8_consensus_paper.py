#!/usr/bin/env python3
"""V8 BTC 15-minute consensus paper trader. NO LIVE ORDER EXECUTION."""
import csv, json, os, threading, time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import requests

HOST=os.getenv("V8_HOST","0.0.0.0")
PORT=int(os.getenv("PORT", os.getenv("V8_PORT","8080")))
SECRET=os.getenv("V8_SHARED_SECRET","").strip()
BASE_URL="https://external-api.kalshi.com/trade-api/v2"
SERIES="KXBTC15M"
FRESH_SECONDS=float(os.getenv("V8_FRESH_SECONDS","90"))
CONSENSUS_WAIT_SECONDS=float(os.getenv("V8_CONSENSUS_WAIT_SECONDS","25"))
POLL_SECONDS=float(os.getenv("V8_SETTLEMENT_POLL_SECONDS","30"))
FEE_PER_CONTRACT=float(os.getenv("V8_FEE_PER_CONTRACT","0.00"))
MAX_ENTRY_PRICE=float(os.getenv("V8_MAX_ENTRY_PRICE","0.60"))
MIN_ENTRY_PRICE=float(os.getenv("V8_MIN_ENTRY_PRICE","0.05"))
DATA=Path(os.getenv("V8_DATA_DIR","."))
DATA.mkdir(parents=True,exist_ok=True)
SIGNALS=DATA/'v8_signals.csv'; TRADES=DATA/'v8_paper_trades.csv'; STATS=DATA/'v8_performance.csv'; DECISIONS=DATA/'v8_consensus_log.csv'
LOCK=threading.RLock(); latest={}; processed=set(); session=requests.Session()

SIGNAL_FIELDS=['received_at','bot','ticker','decision','entry_price','edge','seconds_left','timestamp']
TRADE_FIELDS=['entered_at','ticker','side','entry_price','source_v5_price','source_v6_3_price','source_v7_price','consensus_at','settled','settlement_result','won','gross_pnl','fees','net_pnl','settled_at']
DECISION_FIELDS=['timestamp','ticker','v5_decision','v6_3_decision','v7_decision','consensus','reason']

def now_iso(): return datetime.now(timezone.utc).isoformat()
def write_row(path, fields, row):
    with open(path,'a',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore')
        if f.tell()==0: w.writeheader()
        w.writerow({k:row.get(k,'') for k in fields})
def read_rows(path):
    if not path.exists(): return []
    try:
        with open(path,newline='',encoding='utf-8') as f: return list(csv.DictReader(f))
    except Exception: return []
def parse_bool(v): return str(v).lower() in ('1','true','yes')
def stats():
    settled=[r for r in read_rows(TRADES) if parse_bool(r.get('settled'))]
    wins=sum(1 for r in settled if parse_bool(r.get('won')))
    losses=sum(1 for r in settled if r.get('won','').lower()=='false' or r.get('won','').lower()=='no')
    gross=sum(float(r.get('gross_pnl') or 0) for r in settled)
    fees=sum(float(r.get('fees') or 0) for r in settled)
    net=sum(float(r.get('net_pnl') or 0) for r in settled)
    win_pnls=[float(r.get('net_pnl') or 0) for r in settled if parse_bool(r.get('won'))]
    loss_pnls=[float(r.get('net_pnl') or 0) for r in settled if r.get('won','').lower() in ('false','no')]
    profit=sum(x for x in (float(r.get('net_pnl') or 0) for r in settled) if x>0)
    loss_abs=-sum(x for x in (float(r.get('net_pnl') or 0) for r in settled) if x<0)
    equity=peak=0.0; max_dd=0.0
    for r in sorted(settled,key=lambda x:x.get('settled_at','')):
        equity+=float(r.get('net_pnl') or 0); peak=max(peak,equity); max_dd=max(max_dd,peak-equity)
    alltrades=read_rows(TRADES)
    pending=sum(1 for r in alltrades if not parse_bool(r.get('settled')))
    decisions=read_rows(DECISIONS)
    agrees=sum(1 for r in decisions if r.get('consensus') in ('BUY UP','BUY DOWN'))
    n=len(settled)
    s={'updated_at':now_iso(),'settled_trades':n,'open_paper_trades':pending,'wins':wins,'losses':losses,
       'win_rate':(wins/n if n else 0),'gross_pnl':round(gross,6),'fees':round(fees,6),'net_pnl':round(net,6),
       'average_win':round(sum(win_pnls)/len(win_pnls),6) if win_pnls else 0,
       'average_loss':round(sum(loss_pnls)/len(loss_pnls),6) if loss_pnls else 0,
       'profit_factor':round(profit/loss_abs,6) if loss_abs else ('inf' if profit else 0),
       'max_drawdown':round(max_dd,6),'consensus_trade_decisions':agrees,'consensus_decisions_total':len(decisions),
       'fees_per_contract_assumption':FEE_PER_CONTRACT,'mode':'PAPER ONLY'}
    fields=list(s.keys())
    with open(STATS,'w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerow(s)
    return s

def notify(text):
    token=os.getenv('TELEGRAM_BOT_TOKEN','').strip()
    chat=os.getenv('TELEGRAM_CHAT_ID','').strip()
    if not token or not chat: return
    try: session.post(f'https://api.telegram.org/bot{token}/sendMessage',json={'chat_id':chat,'text':text},timeout=5).raise_for_status()
    except Exception as e: print('V8 Telegram report error:',repr(e))

def log_decision(ticker, signals, consensus, reason):
    row={'timestamp':now_iso(),'ticker':ticker,'v5_decision':signals.get('V5',{}).get('decision','MISSING'),
         'v6_3_decision':signals.get('V6.3',{}).get('decision','MISSING'),'v7_decision':signals.get('V7',{}).get('decision','MISSING'),
         'consensus':consensus,'reason':reason}
    write_row(DECISIONS,DECISION_FIELDS,row)

def maybe_consensus(ticker):
    with LOCK:
        sigs=latest.get(ticker,{})
        now=time.time()
        fresh={k:v for k,v in sigs.items() if now-float(v.get('timestamp',0)) <= FRESH_SECONDS}
        if len(fresh)<3:
            oldest=min((float(v.get('timestamp',now)) for v in sigs.values()),default=now)
            if now-oldest >= CONSENSUS_WAIT_SECONDS and ticker not in processed:
                log_decision(ticker,fresh,'NO TRADE','missing or stale bot signal')
                processed.add(ticker)
            return
        if ticker in processed: return
        decisions=[fresh.get(k,{}).get('decision','NO TRADE') for k in ('V5','V6.3','V7')]
        if len(set(decisions))!=1 or decisions[0] not in ('BUY UP','BUY DOWN'):
            log_decision(ticker,fresh,'NO TRADE','bots disagree or at least one says NO TRADE')
            processed.add(ticker); return
        side=decisions[0]
        prices=[]
        for bot in ('V5','V6.3','V7'):
            try: prices.append(float(fresh[bot].get('entry_price')))
            except (TypeError,ValueError): prices.append(None)
        usable=[p for p in prices if p is not None and MIN_ENTRY_PRICE<=p<=MAX_ENTRY_PRICE]
        if len(usable)<3:
            log_decision(ticker,fresh,'NO TRADE','missing/out-of-range entry price from one or more bots')
            processed.add(ticker); return
        # Conservative paper entry: use the highest of the three quoted prices.
        entry=max(usable)
        if ticker in {r.get('ticker') for r in read_rows(TRADES)}:
            log_decision(ticker,fresh,'NO TRADE','ticker already has a V8 paper trade')
            processed.add(ticker); return
        row={'entered_at':now_iso(),'ticker':ticker,'side':side,'entry_price':f'{entry:.6f}',
             'source_v5_price':f'{prices[0]:.6f}','source_v6_3_price':f'{prices[1]:.6f}','source_v7_price':f'{prices[2]:.6f}',
             'consensus_at':now_iso(),'settled':'false','settlement_result':'','won':'','gross_pnl':'','fees':'','net_pnl':'','settled_at':''}
        write_row(TRADES,TRADE_FIELDS,row); log_decision(ticker,fresh,side,'all three fresh signals agree')
        processed.add(ticker)
        msg=(f'🟣 V8 CONSENSUS PAPER ENTRY\n{side}\nTicker: {ticker}\nConservative entry: ${entry:.2f}\n'
             f'V5/V6.3/V7 prices: ${prices[0]:.2f} / ${prices[1]:.2f} / ${prices[2]:.2f}\n'
             'Three-way agreement confirmed. PAPER ONLY — no live order was sent.')
        notify(msg); print(msg.replace('\n',' | '))
        s=stats(); notify(f"V8 PAPER STATS\nRecord: {s['wins']}-{s['losses']} ({s['win_rate']:.1%})\nSettled: {s['settled_trades']} | Open: {s['open_paper_trades']}\nGross P&L: {s['gross_pnl']:+.4f} | Fees: {s['fees']:.4f} | Net P&L: {s['net_pnl']:+.4f}\nMax drawdown: {s['max_drawdown']:.4f}\nPAPER ONLY")

def receive_signal(payload):
    bot=str(payload.get('bot','')).strip().upper()
    if bot=='V6.3': bot='V6.3'
    if bot not in ('V5','V6.3','V7'): raise ValueError('unknown bot')
    ticker=str(payload.get('ticker','')).strip()
    decision=str(payload.get('decision','NO TRADE')).strip().upper()
    if not ticker or not ticker.startswith(SERIES): raise ValueError('invalid/non-KXBTC15M ticker')
    if decision not in ('BUY UP','BUY DOWN','NO TRADE'): decision='NO TRADE'
    ts=float(payload.get('timestamp') or time.time())
    if abs(time.time()-ts)>300: raise ValueError('timestamp outside accepted window')
    signal={'bot':bot,'ticker':ticker,'decision':decision,'entry_price':payload.get('entry_price'),
            'edge':payload.get('edge'),'seconds_left':payload.get('seconds_left'),'timestamp':ts,'received_at':now_iso()}
    with LOCK:
        latest.setdefault(ticker,{})[bot]=signal
        write_row(SIGNALS,SIGNAL_FIELDS,signal)
    maybe_consensus(ticker)

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ('/','/health','/stats'):
            self.send_error(404); return
        body=json.dumps({'service':'V8 BTC consensus','mode':'PAPER ONLY','status':'ok','stats':stats() if self.path=='/stats' else None}).encode()
        self.send_response(200); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_POST(self):
        if self.path!='/signal': self.send_error(404); return
        if not SECRET or self.headers.get('X-V8-Secret','')!=SECRET:
            self.send_error(401); return
        try:
            length=int(self.headers.get('Content-Length','0'))
            if length<1 or length>20000: raise ValueError('invalid body size')
            payload=json.loads(self.rfile.read(length).decode('utf-8'))
            receive_signal(payload)
            body=b'{"ok":true,"mode":"PAPER ONLY"}'
            self.send_response(200); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
        except Exception as e:
            body=json.dumps({'ok':False,'error':str(e)}).encode(); self.send_response(400); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self,fmt,*args): print('[HTTP]',fmt%args)

def settle_loop():
    while True:
        try:
            rows=read_rows(TRADES); changed=False
            for row in rows:
                if parse_bool(row.get('settled')): continue
                ticker=row.get('ticker','')
                try:
                    r=session.get(f'{BASE_URL}/markets/{ticker}',timeout=8)
                    if r.status_code==404: continue
                    r.raise_for_status(); market=r.json().get('market',r.json())
                    result=str(market.get('result','')).strip().lower()
                    if result not in ('yes','no'): continue
                    won=(row.get('side')=='BUY UP' and result=='yes') or (row.get('side')=='BUY DOWN' and result=='no')
                    entry=float(row.get('entry_price') or 0)
                    gross=(1.0-entry) if won else -entry
                    fee=FEE_PER_CONTRACT
                    row.update({'settled':'true','settlement_result':result.upper(),'won':'true' if won else 'false',
                                'gross_pnl':f'{gross:.6f}','fees':f'{fee:.6f}','net_pnl':f'{gross-fee:.6f}','settled_at':now_iso()})
                    changed=True
                    notify(f"{'✅' if won else '❌'} V8 PAPER SETTLED\n{ticker} | {row.get('side')}\nResult: {result.upper()} | {'WIN' if won else 'LOSS'}\nEntry: ${entry:.2f}\nGross: {gross:+.4f} | Fees: {fee:.4f} | Net: {gross-fee:+.4f}\nPAPER ONLY")
                except Exception as e: print('V8 settlement check failed:',ticker,repr(e))
            if changed:
                with open(TRADES,'w',newline='',encoding='utf-8') as f:
                    w=csv.DictWriter(f,fieldnames=TRADE_FIELDS); w.writeheader(); w.writerows([{k:r.get(k,'') for k in TRADE_FIELDS} for r in rows])
                s=stats()
                notify(f"📊 V8 PERFORMANCE UPDATE\nRecord: {s['wins']}-{s['losses']} | Win rate: {s['win_rate']:.1%}\nSettled: {s['settled_trades']} | Open: {s['open_paper_trades']}\nGross P&L: {s['gross_pnl']:+.4f} | Fees: {s['fees']:.4f} | Net P&L: {s['net_pnl']:+.4f}\nAvg win: {s['average_win']:+.4f} | Avg loss: {s['average_loss']:+.4f}\nProfit factor: {s['profit_factor']} | Max drawdown: {s['max_drawdown']:.4f}\nPAPER ONLY")
        except Exception as e: print('V8 settlement loop error:',repr(e))
        time.sleep(POLL_SECONDS)

def main():
    if not SECRET: raise SystemExit('Set V8_SHARED_SECRET to a long random secret before starting V8.')
    threading.Thread(target=settle_loop,daemon=True).start()
    print(f'V8 consensus PAPER ONLY listening on {HOST}:{PORT}; required bots=V5,V6.3,V7; fee/contract={FEE_PER_CONTRACT}')
    print('No Kalshi authenticated credentials or live-order endpoints are used.')
    ThreadingHTTPServer((HOST,PORT),Handler).serve_forever()
if __name__=='__main__': main()
