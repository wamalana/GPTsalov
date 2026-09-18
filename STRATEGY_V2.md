# Strategy review และ v2 candidates — 18 ก.ย. 2026 (Claude)

เอกสารนี้วิเคราะห์กลยุทธ์ `hourly_ema20_donchian20_volume_v1` และเพิ่มเครื่องมือ **historical backtest**
ที่ repo ยังไม่มี พร้อม candidate รุ่น v2 แบบ research-only
**ไม่มีการแก้ paper engine, config, risk lock, Testnet หรือ service ที่รันอยู่** และยังไม่มีผลบนข้อมูลจริง
เพราะ sandbox ที่ใช้พัฒนาเข้า Binance ไม่ได้ ต้องไปรันบน VPS (ขั้นตอนอยู่ท้ายเอกสาร)

## 1. สรุปปัญหา (เรียงตามผลกระทบ)

### 1.1 ต้นทุนกินกำไรทั้งหมด — ปัญหาหลัก
ต้นทุนไป-กลับตามโมเดล = fee 5+5 + slippage 3+3 + funding reserve 10 = **26 bps ของ notional**
แต่ stop = 1.5 × ATR(15m) และตัวกรองยอม ATR ต่ำสุด 0.2% → stop แคบได้ถึง 0.3%

| stop distance | net reward/risk ของเป้า 2R | win rate ขั้นต่ำเพื่อเท่าทุน |
|---:|---:|---:|
| 0.30% | 0.61 | 62% |
| 0.50% | 0.97 | 51% |
| 0.75% | 1.23 | 45% |
| 1.00% | 1.38 | 42% |
| 1.50% | 1.56 | 39% |

(ประมาณ: reward = 2d − 0.26%, risk = d + 0.26%) กลยุทธ์ breakout ทั่วไปชนะราว 35–45%
จึงติดลบเชิงโครงสร้างในช่วงตลาดนิ่ง หลักฐานที่ยืนยัน:

- **ผล audit VPS (TUNING.md, 13 ก.ย.)**: baseline 18 เทรด gross **+0.99** USDT แต่ fee+reserve **2.11** → net **−1.12**
  สัญญาณมี gross เป็นบวก แต่ต้นทุนเป็น 213% ของ gross
- **Backtest บน random walk (ไม่มี edge)**: v1 เสีย **−0.29R ต่อเทรด** (CI95 −0.32…−0.25, 2,940 เทรด)
  = สัญญาณต้องมี edge มากกว่า 0.29R ต่อเทรดแค่เพื่อเท่าทุน ขณะที่ candidate 1h เสียเพียง ~−0.1R

### 1.2 Stop ถูกบีบจากการเข้าช้าหนึ่งแท่ง (bug เชิงออกแบบ)
Stop/target คำนวณจากราคาปิดแท่งสัญญาณ แต่เข้าจริงที่ราคาเปิดแท่งถัดไป และ drift guard อนุญาต 0.5% แบบค่าคงที่
ซึ่ง**ใหญ่กว่า stop ทั้งก้อน**ได้ ตัวอย่างจริง: LINK short ระยะ entry→stop เหลือแค่ **7.6%** ของที่วางแผน
แต่ระบบรายงาน net RR 3.59 แล้วโดน stop ทันที
**แก้**: `reanchor` — วัด stop/target จากราคาเข้าจริง และยกเลิกถ้าราคาวิ่งสวนไปแล้วเกิน 25% ของระยะ stop

### 1.3 Timeframe 15m ไม่เข้ากับโครงสร้างต้นทุน
ATR ของแท่ง 1h ราว 2 เท่าของ 15m ขณะที่ต้นทุนต่อเทรดคงที่ → cost share ลดลงครึ่งหนึ่ง
และสัญญาณ breakout บน 15m มี noise สูง (false breakout บ่อย)

### 1.4 การจัดอันดับเลือกตัวที่ "วิ่งไปไกลแล้ว"
`score = |close − EMA| / ATR` เลือกเหรียญที่ยืดตัวมากที่สุดก่อน = ไล่ราคา มีโอกาส mean-revert สูง
v2 ใช้ efficiency ratio × volume surge แทน และตัดสัญญาณที่ปิดเกินแนว breakout > 0.75 ATR

### 1.5 Multi-agent ยังไม่ได้เพิ่มข้อมูลใหม่
Agent trend/momentum/volatility ตรวจเงื่อนไข**ชุดเดียวกับ** `core.strategy` ซ้ำ (GPT ระบุไว้เองใน RISK_AWARE_STRATEGY.md)
การที่ทุก agent เห็นตรงกันจึงไม่ใช่หลักฐานอิสระ ข้อสังเกตเพิ่ม: `multiagent_shadow.py` บน main import
`.ai_review` ที่มีอยู่แค่ใน branch `feat/risk-aware-leverage` → ถ้าใส่ `--ai-config` บน main จะ crash

### 1.6 ไม่มี backtest → forward test ช้าเกินจะตัดสินได้
R ต่อเทรดมี SD ราว 1.0 → ต้องใช้ ~**400 เทรด**จึงจะแยก edge 0.1R ออกจาก noise ได้ (2 SE)
forward ledger ได้ไม่กี่เทรดต่อวัน และแต่ละกลุ่มโดน LOSS_STREAK lock ไปแล้ว การตัดสินจาก 5–18 เทรดคือการเดา
ต้องทดสอบย้อนหลังหลายปีก่อน แล้วใช้ forward/Testnet เพื่อยืนยันการ execute เท่านั้น

### 1.7 Funding reserve 10 bps คงที่
Funding ปกติ ~1 bp ต่อ 8 ชม. ถือ ≤ 4 ชม. จึงถูกเผื่อเกินจริง ~10 เท่า (อนุรักษ์นิยมแต่บิดเบือนการเปรียบเทียบ)
backtest ใช้ funding rate ย้อนหลังจริงจาก archive เมื่อมีไฟล์ ถ้าไม่มีจะใช้ reserve เดิม

## 2. สิ่งที่เพิ่มใน branch นี้

| ไฟล์ | หน้าที่ |
|---|---|
| `gptsalov/backtest.py` | ดาวน์โหลดแท่ง 15m + funding จาก data.binance.vision, replay portfolio แบบ one-position, แยก in-sample/out-of-sample, bootstrap CI, stress ต้นทุน |
| `gptsalov/strategy_v2.py` | สัญญาณ candidate v2 (pure functions, float, ไม่มีสิทธิ์ส่งคำสั่ง) |
| `tests/test_backtest.py` | 9 tests: parity กับ `core.strategy` จริง, no-lookahead, stop-first/gap, cost gate, random-walk sanity |

**Baseline ใน backtest คือโค้ด production ตัวจริง** (`core.strategy` บนหน้าต่าง 199 แท่งเดียวกับ live scan)
มี test ยืนยันว่า pre-filter ไม่ทำสัญญาณหาย

### Variants ที่ประกาศล่วงหน้า (ไม่ได้ fit กับข้อมูล)

| ชื่อ | สัญญาณ | การเปลี่ยนแปลง |
|---|---|---|
| `v1_baseline` | v1 15m | เหมือน production |
| `v1_reanchor` | v1 15m | stop/target จากราคาเข้าจริง, ยกเลิกถ้าสวน > 0.25 stop |
| `v1_reanchor_cost` | v1 15m | + cost gate: ต้นทุนไป-กลับ ≤ 25% ของระยะ stop |
| `v2_1h_fixed2R` | v2 1h | breakout 20 แท่ง 1h + EMA50 ชัน + ER ≥ 0.3 + close location ≥ 0.6 + ไม่ไล่ > 0.75 ATR + ATR ยังไม่ขยาย > 1.3× + ทิศเดียวกับ BTC; เป้า 2R, ถือ ≤ 24 ชม. |
| `v2_1h_trail` | v2 1h | สัญญาณเดียวกัน, ไม่มีเป้าคงที่: breakeven ที่ +1R แล้ว trail 2.5 ATR, ถือ ≤ 48 ชม. |

เหตุผลของ v2: breakout มีกำไรจาก "หางขวา" ของเทรนด์ การตัดที่ 2R + ปิดใน 4 ชม. ตัดหางนั้นทิ้ง
ขณะที่ต้นทุนยังจ่ายเต็ม ตัวกรอง BTC ลดการเปิด alt ทวนทิศตลาดรวม (alt correlation กับ BTC สูง)

## 3. ผลตรวจบนข้อมูลสังเคราะห์ (ไม่ใช่หลักฐานกำไร)

Random walk 12 เหรียญ × 30,000 แท่ง 15m — ไม่มี edge ใด ๆ ดังนั้นทุก variant **ควร** ติดลบเท่าต้นทุน:

| variant | เทรด | avg R | CI95 |
|---|---:|---:|---|
| v1_baseline | 2,940 | −0.288 | −0.32…−0.25 |
| v1_reanchor | 2,971 | −0.284 | −0.32…−0.25 |
| v1_reanchor_cost | 1,754 | −0.131 | −0.18…−0.08 |
| v2_1h_fixed2R | 265 | −0.116 | −0.25…+0.03 |
| v2_1h_trail | 216 | −0.080 | −0.26…+0.11 |

ตีความ: harness ไม่มี lookahead bias (ไม่มี variant ไหน "ชนะ" random walk) และแสดงว่า v2 ลด
ต้นทุนเชิงโครงสร้างจาก ~0.29R เหลือ ~0.1R ต่อเทรด **edge จริงต้องวัดบนข้อมูลตลาดเท่านั้น**

## 4. ขั้นตอนรันบน VPS

```bash
cd ~/GPTsalov/<release ที่มี branch นี้>
python3 -m unittest discover -s tests -q
# ~20 เหรียญ × 32 เดือน ≈ 250 MB CSV; โหลดครั้งเดียว ข้ามไฟล์ที่มีแล้ว
python3 -m gptsalov.backtest download --dir ~/GPTsalov/data/hist --start 2024-01 --end 2026-08
python3 -m gptsalov.backtest run --dir ~/GPTsalov/data/hist --split 2026-01-01 --out bt_base.json
python3 -m gptsalov.backtest run --dir ~/GPTsalov/data/hist --split 2026-01-01 --cost-mult 1.5 --out bt_stress.json
```

ใช้แค่ไฟล์ static สาธารณะ ไม่ใช้ API key ไม่แตะ ledger/service ที่รันอยู่ ควรรันด้วย `nice` เพราะใช้ CPU หลายนาที

## 5. เกณฑ์ก่อนนำ variant ไปทดสอบ forward (ประกาศก่อนเห็นผล)

1. เลือก variant โดยดู **in-sample (ก่อน 2026-01-01) เท่านั้น** ห้ามปรับพารามิเตอร์หลังเห็น out-of-sample
2. Out-of-sample: avg R > 0 **และ** ≥ 100 เทรด **และ** profit factor > 1.15
3. `--cost-mult 1.5` ยังไม่ติดลบ
4. ไม่พึ่งเหรียญเดียว/เดือนเดียว: ตัดเหรียญที่กำไรมากสุดออกแล้ว avg R ยัง ≥ 0
5. Long และ short ประเมินแยก — ถ้า short ติดลบชัดใน **ทั้งสองช่วง** จึงพิจารณา long-only (ไม่ตัดสินจาก 18 เทรด)

ผ่านแล้วจึงเสียบเป็น `validate_entry`/signal ใน forward ledger ชุดใหม่ (ไม่ reset ของเดิม) แล้วค่อย Testnet
ถ้าไม่มีตัวไหนผ่าน = ข้อมูลบอกว่าไม่มี edge ให้เปลี่ยนแนวคิดสัญญาณ ไม่ใช่เพิ่ม leverage หรือ filter ซ้อน

## 6. ไอเดียถัดไป (ยังไม่ทำ — ทดสอบทีละข้อด้วย harness นี้)

- **Maker entry**: ตั้ง limit ที่แนว breakout เดิม (retest) แทน market ที่ open ถัดไป → fee 2 bps แทน 5 และราคาเข้าดีกว่า แลกกับพลาดบางเทรด
- **Funding/OI filter**: ไม่ long เมื่อ funding สูงผิดปกติ (ฝั่งเดียวแน่น) ต้องการ OI เพิ่มยืนยัน breakout (archive มี metrics รายวัน)
- **Regime switch**: ER ต่ำต่อเนื่อง = sideway → ใช้ mean-reversion แยกเป็น strategy อีกตัว ไม่ใช่รวมคะแนน vote
- **Time-of-day**: วัด avg R ตามชั่วโมง UTC จาก backtest ก่อนตัดช่วงใด ๆ

## ข้อจำกัดของ backtest

ไม่ปัดตาม step/min notional ของ exchange, fill เต็มจำนวน, universe = เหรียญที่ดาวน์โหลด (survivorship bias —
เหรียญที่ถูกถอดไม่อยู่ในชุด), float แทน Decimal, risk lock นับจำนวนครั้งแต่ไม่หยุดเทรด, ไม่มี liquidation model
ผลจึงเป็นการเปรียบเทียบ variant ภายใต้สมมติฐานเดียวกัน ไม่ใช่การคาดการณ์ผลตอบแทน
