# GPTsalov v0.1.2 — เริ่มรันระบบทดลอง

v0.1.2 เพิ่มการเก็บข้อมูลและประเมิน net reward/risk แบบ observation-only
ผ่าน `--research-db` โดยไม่เปลี่ยนกลยุทธ์หรือ config เดิม ดู [RESEARCH.md](RESEARCH.md)
รายงาน JSON เพิ่มเวลาปิดเทรด อายุการสแกน และผลเทรดที่ปิดใน 4 ชั่วโมงล่าสุด
การติดตั้ง source/unit รุ่นนี้บน VPS เป็นขั้นตอนแยกจากการ push โค้ด

สร้างวันที่ 9 กันยายน 2026 สำหรับโปรเจกต์ Binance Futures ทุนเริ่มต้น $100

**รุ่นนี้เป็น paper-trading research prototype ไม่ส่งคำสั่งเงินจริง ไม่รับ API key และยังไม่ใช่ระบบ AI ที่พิสูจน์กำไรแล้ว**

## สิ่งที่ทำงานแล้ว

- คำสั่ง `scan`: อ่าน Binance public API เพื่อคัดกรองคริปโต USDT perpetual ทั้งตลาด จากสถานะสัญญา อายุ ปริมาณซื้อขาย spread และสภาพคล่องระดับ bid/ask แรก แล้วดึงแท่งเทียนละเอียดเฉพาะ 20 คู่แรกตาม volume
- Strategy baseline ตัวเดียว: แนวโน้ม EMA20 จากแท่ง 1h ที่ปิดครบแล้ว + breakout กรอบ 20 แท่ง 15m + volume confirmation รองรับ Long/Short; ATR ใช้กำหนด stop/target
- Risk engine ใช้ Decimal คำนวณ quantity จากวงเงินขาดทุน ปัดตาม filter ของแต่ละคู่ และปฏิเสธถ้าขนาดขั้นต่ำใหญ่เกินงบ
- บัญชีจำลอง $100 เปิดสูงสุดหนึ่งสถานะ มี entry/exit cost, slippage และ funding reserve สมมติ
- ล็อกขาดทุนรายวัน 2%, drawdown 8% และแพ้ติดกัน 3 ไม้; รีสตาร์ตแล้วไม่ล้างสถานะหรือเพดานขาดทุน
- เก็บ state/event ใน SQLite แบบ transaction พร้อมล็อก single writer ป้องกันสอง process ใช้บัญชีเดียวกัน
- มี CLI status และรายงาน JSON พร้อม timestamp/error/health; ไม่มีเว็บ dashboard ในรุ่นนี้
- มีข้อมูลสังเคราะห์ offline, tests และไฟล์ Docker Compose สำหรับนำไปรัน paper process ต่อบน Linux

คะแนนคัดเลือกเป็น heuristic ไม่ใช่โอกาสชนะหรือกำไรคาดหวังที่ผ่าน calibration และไม่ได้รับประกันว่าเลือกเหรียญดีที่สุด

## Source repository และ VPS

โค้ดอยู่ที่ [wamalana/GPTsalov](https://github.com/wamalana/GPTsalov) เป็น private repository ต้องใช้บัญชีหรือ SSH key ที่ได้รับสิทธิ์ก่อน clone ดู [DEPLOY.md](DEPLOY.md) สำหรับขั้นตอนนำไปเริ่มรันบน VPS ด้วยข้อมูล Binance และพอร์ตจำลอง

```bash
git clone git@github.com:wamalana/GPTsalov.git
cd GPTsalov
```

เมื่อ clone แล้วให้รันคำสั่งถัดไปจาก root ของ repository ได้เลย ไม่ต้องเข้าโฟลเดอร์ Python package ซ้ำ

## เริ่มใน 3 คำสั่ง — ไม่ต้องใช้ API key

ต้องใช้ Linux หรือ macOS, Python 3.11 ขึ้นไป พร้อม timezone database ของระบบ คำสั่งด้านล่างให้รันหลังแตก ZIP แล้วเข้าโฟลเดอร์ `gptsalov` บน Windows ให้ใช้ WSL2 หรือ Docker เพราะมีการล็อกไฟล์ด้วย fcntl

```bash
python3 -m unittest discover -s tests -v
python3 -m gptsalov run --source demo --config config.toml --db data/demo.db --cycles 180
python3 -m gptsalov status --db data/demo.db
```

ไม่ต้อง `pip install` เมื่อรันจากโฟลเดอร์นี้ เพราะโค้ด runtime ใช้ Python standard library ทั้งหมด

Demo เดินเวลาเสมือนเร็ว ไม่ได้รอ 15 นาทีต่อรอบ ถ้ารันต่อโดยใช้ฐานข้อมูลเดิม โปรแกรมจะเดินต่อจากจุดเดิม ไม่ใช่เริ่มทดสอบใหม่ ชุดข้อมูลมี 500 แท่งรวมช่วง warm-up; ถ้าข้อมูลหมดโปรแกรมจะหยุดพร้อมแจ้งเหตุผล ถ้าต้องการทดลองใหม่ให้ใช้ชื่อฐานข้อมูลใหม่และเก็บผลเดิมไว้

## อ่าน Binance จริงโดยยังไม่ซื้อขาย

ตรวจว่าบัญชีและสถานที่ใช้งานของคุณมีสิทธิ์ใช้ผลิตภัณฑ์ตามเงื่อนไข Binance ก่อน ข้อมูล public อ่านได้ไม่ได้หมายความว่าบัญชีมีสิทธิ์ส่งคำสั่ง Futures และการเลือก region VPS ไม่ได้เปลี่ยนสิทธิ์ผู้ใช้

```bash
python3 -m gptsalov check-access
python3 -m gptsalov scan --source binance --config config.toml
```

`check-access` เรียก server time เพียงครั้งเดียว ส่วน `scan` ไม่สร้างสถานะและไม่อ่านบัญชี

ในสภาพแวดล้อมที่ใช้พัฒนานี้ การทดสอบเชื่อมต่อจริงไม่สำเร็จเพราะการอนุญาตเครือข่ายถูกยกเลิกก่อนมีผล จึงยังยืนยัน end-to-end กับ Binance ไม่ได้ ไม่ได้สรุปว่า Binance เป็นผู้ปฏิเสธหรือว่าบัญชีของคุณถูกบล็อก ตัวรับข้อมูลถูกทดสอบด้วย mock ตามรูปแบบ API และต้องตรวจจากเครื่องที่มีการเชื่อมต่อได้รับอนุญาตก่อนใช้งานต่อ

## จำลองเทรดต่อเนื่องด้วยข้อมูล Binance

หลัง `check-access` และ `scan` ผ่านแล้ว:

```bash
python3 -m gptsalov run --source binance --config config.toml --db data/binance-paper.db --cycles 0
```

- `--cycles 0` หมายถึงทำงานต่อจนกด Ctrl+C หรือเกิดข้อผิดพลาด; ไม่ได้เริ่มโปรแกรมทิ้งไว้ให้ใน session นี้
- อ่านข้อมูลทุก 60 วินาทีโดย default แต่ประมวลผลการจำลองหนึ่งครั้งต่อแท่ง 15m ที่ปิดใหม่
- โปรแกรมล็อก source ของฐานข้อมูล: ห้ามใช้ `demo.db` ปนกับข้อมูล Binance
- ถ้า HTTP 403/418/429/451, network error, clock skew หรือข้อมูลที่จำเป็นใช้ไม่ได้ โปรแกรมจะหยุด ไม่วนเปลี่ยน IP หรือหาช่องเลี่ยงการปฏิเสธ
- เมื่อเกิดข้อผิดพลาดให้อ่านรายงานและแก้สาเหตุก่อนเริ่ม process ใหม่ ไม่ตั้ง auto-retry เพื่อฝืน access/rate-limit block
- ถ้าหายไปหลายแท่งขณะมีสถานะจำลอง โปรแกรมจะล็อก `DATA_GAP_REQUIRES_RECONCILIATION` และเก็บ mark เก่า ไม่แต่งราคา exit ขึ้นเอง รุ่นนี้ยังไม่มีเครื่องมือ reconcile/unlock ให้ผู้ใช้กดข้าม
- ถ้าหายไปขณะไม่มีสถานะ โปรแกรมทิ้ง intent เก่าและเริ่มสังเกตข้อมูลปัจจุบันใหม่ พร้อม event `FLAT_RESYNC`

อาจไม่มีเทรดหลายชั่วโมงหรือหลายวันได้ตามสัญญาณและตัวกรอง นั่นไม่ใช่เหตุผลให้เพิ่มความเสี่ยงหรือบังคับเทรด

## วิธีอ่านรายงาน

```bash
python3 -m gptsalov status --db data/binance-paper.db --json
```

รายงานนี้อ่านฐานข้อมูลเท่านั้น ไม่เชื่อมตลาดใหม่และไม่ส่งคำสั่ง ให้ตรวจ `source`, `as_of_ms`, `data_age_ms`, `last_error`, `health` ก่อนตีความ equity

| ฟิลด์ | ความหมาย |
|---|---|
| balance | ยอดจำลองหลัง realized PnL, ค่าธรรมเนียมที่เกิดขึ้น และ reserve ที่เรียกเก็บ |
| equity | balance บวกกำไร/ขาดทุนค้าง รวมเผื่อ slippage/fee สำหรับออก |
| net_pnl_estimate | equity ลบทุนเริ่มต้น ยังไม่หักค่า VPS/AI/news |
| fees | ค่าธรรมเนียมตามสมมติฐาน config ไม่ใช่ค่าที่อ่านจากบัญชี Binance |
| funding_reserve_charged | ต้นทุนเผื่อคงที่ต่อเทรด ไม่ใช่ funding payment จริง |
| daily_locked / hard_lock | การล็อกเปิดไม้ใหม่จากกฎความเสี่ยง |
| health | แยกข้อมูลสังเคราะห์ ข้อมูลเก่า ข้อผิดพลาด และสถานะล็อก |
| last_scan.rejected | เหตุผลที่เหรียญไม่ผ่านตัวกรองหรือไม่มีสัญญาณ |

timestamp ภายในเป็น UTC milliseconds ส่วนวันบัญชีใช้ Asia/Bangkok การหยุดจาก loss streak/drawdown ไม่มีคำสั่งปลดล็อกอัตโนมัติในรุ่นนี้

## ค่าความเสี่ยง

ไฟล์ `config.toml` มีค่าตั้งต้นตามสเปก: risk 0.5% ต่อไม้, notional สูงสุดเท่ากับ equity และ leverage จำลอง 2x

Quantity คำนวณจาก budget หารความเสียหายต่อเหรียญเมื่อ stop รวม exit slippage, entry/exit fees และ funding reserve จากนั้นปัดลงและตรวจขั้นต่ำ ไม่เพิ่ม leverage เพื่อแก้ปัญหาขนาดขั้นต่ำ

ค่าธรรมเนียม 5 bps/ข้าง, slippage 3 bps/ข้าง และ funding reserve 10 bps/เทรด **เป็นสมมติฐานวิจัย ไม่ใช่อัตราปัจจุบันที่ยืนยันแล้ว** ควรทำ sensitivity tests ก่อนสรุปผล การเปลี่ยน config แล้วใช้ฐานข้อมูลเก่าจะถูกปฏิเสธเพื่อไม่ให้การเปลี่ยนความเสี่ยงเกิดขึ้นเงียบ ๆ

## ขอบเขตและข้อจำกัดของ simulator

1. ใช้แท่งเทียนที่ปิดแล้วเท่านั้น สัญญาณต้องสด และบันทึก intent ก่อนช่วงราคาเข้า: เลือกราคาเปิดของแท่งในอนาคตที่เริ่มหลังเวลาสังเกตสัญญาณอย่างเคร่งครัด จึงมีการหน่วงอย่างน้อยหนึ่งช่วง 15m ไม่ใช่ scalping simulator
2. Entry fill/ขนาดถูกคำนวณและบันทึกเมื่อแท่งสำหรับเข้าเสร็จแล้วตาม intent ที่ตั้งล่วงหน้า ไม่ใช่คำสั่งที่ส่งจริงตอนเปิดแท่ง ปริมาณ fill จำลองเต็มจำนวน ไม่มี queue/partial fill
3. SL และ TP แตะในแท่งเดียวกันจะถือว่า SL ก่อน ถ้าเปิดกระโดดเลย stop จะใช้ราคาเปิดที่แย่กว่าพร้อม slippage หากเปิดกระโดดข้ามเป้าหมายก่อนเข้า อาจยกเลิก intent ตาม drift/stop-target checks
4. Funding เป็น reserve ที่หักตอนเปิดครั้งเดียว ไม่ใช่ประวัติ funding ตามเวลา จึงยังไม่เหมาะสำหรับประเมินกลยุทธ์ถือข้าม funding อย่างจริงจัง
5. Risk triggers/high-water ประเมินจากค่าปิดแท่งและต้นทุนแบบจำลอง ไม่ใช่ realtime intrabar risk controls อาจพลาดการแตะ threshold ระหว่างแท่งแล้วฟื้นกลับ
6. ไม่จำลอง liquidation, maintenance-margin tiers, mark-vs-contract divergence, ADL, exchange outage execution หรือ stop rejection ห้ามนำ simulator นี้ไปต่อคำสั่งเงินจริงโดยตรง
7. สแกนตลาดปัจจุบัน ไม่ใช่ historical universe backtest; ไม่มีการประเมินเหรียญที่เคยถูกถอดในอดีต ไม่มี walk-forward optimizer หรือช่วงความเชื่อมั่น
8. ไม่มีหลักฐาน out-of-sample ว่ากลยุทธ์ทำกำไร ผล demo ใช้ตรวจบัญชีและลำดับเหตุการณ์เท่านั้น

## นำไปเตรียมรันบน GCP

สำหรับการติดตั้งด้วย Docker ต้องมีโครงการ GCP, งบ, region ที่ใช้งานได้ตามสิทธิ์ และเครื่องที่ติดตั้ง Docker/Compose ก่อน คำสั่งนี้ไม่ต้องใช้ Binance key ส่วนเครื่องที่ใช้ Python โดยตรงสามารถใช้ systemd user service ตาม DEPLOY.md:

```bash
docker compose build
docker compose run --rm paper check-access
docker compose up -d
docker compose logs --tail 30 paper
docker compose exec paper python -m gptsalov status --db /data/paper.db --json
```

ถ้า process หยุดแล้ว ใช้ `docker compose run --rm paper status --db /data/paper.db --json` อ่านรายงานได้ ตัวอย่าง Compose ไม่เปิดพอร์ตเข้าจากอินเทอร์เน็ต ใช้ non-root process, read-only filesystem และ named volume สำหรับบัญชีจำลอง

ตั้ง `restart: "no"` เพื่อไม่วนยิง API หลังถูกปฏิเสธ จึงยังไม่ใช่บริการ production ที่รับประกัน 24/7 ต้องเพิ่ม monitoring, supervised recovery และ backup/reconciliation ในระยะถัดไป ไฟล์ Docker/Compose ได้จัดเตรียมไว้ แต่ยังไม่ได้ build หรือทดสอบบน GCP ในรอบนี้

อย่าใช้คำสั่งลบ Docker volume/ฐานข้อมูลเพื่อแก้ risk lock เพราะจะทำให้ประวัติหาย ต้องเก็บหลักฐานและตรวจสาเหตุก่อน

## งานถัดไป

1. ทดสอบ public API จากเครื่องที่ได้รับอนุญาตและเก็บข้อมูลจริงเพื่อทวนผลการคัดกรอง
2. แยก market collector แบบ WebSocket ออกจาก execution เพิ่ม stale-data watchdog และตรวจ mark price
3. สร้าง realistic replay/backtest รวม funding ตามเวลา, point-in-time universe และ walk-forward validation
4. เพิ่มข่าวพร้อม source/time/dedup แล้วค่อยเชื่อม LLM ให้ช่วยวิเคราะห์โดยไม่มีสิทธิ์ซื้อขาย
5. ทำ exchange demo adapter พร้อม state machine, partial fills, idempotency, TP/SL reconciliation และ fault injection
6. ทดลองเงินจริงเฉพาะเมื่อผ่านเกณฑ์และได้รับการยืนยัน โดยไม่มีทางเปิด live ผ่าน config รุ่นนี้
7. เพิ่ม dashboard และช่องทางรายงานที่มี authentication จากนั้นจึงเชื่อมงานตรวจตามเวลาของ ChatGPT

ยังไม่ได้สร้าง schedule, เชื่อม AI/news, ฝากเงิน, เปิดสถานะจริง หรือจัดซื้อบริการใด ๆ

## การติดตั้งด้วย systemd ของผู้ใช้

เพิ่ม unit `deploy/gptsalov-paper.service` สำหรับเครื่อง Linux ที่มี Python พร้อมอยู่แล้ว ไม่ต้องใช้ Docker ดูขั้นตอนและโครงสร้าง release ใน [DEPLOY.md](DEPLOY.md)

v0.1.1 ปิด SQLite connection หลังอ่านรายงาน และข้ามรอบที่ snapshot คาบเกี่ยวเวลาปิดแท่งหรือใช้เวลาเกินกำหนด โดยรอรอบ polling ถัดไป ข้อผิดพลาดด้านสิทธิ์เครือข่าย/HTTP block ยังหยุดโปรแกรมตามเดิม

## โครงสร้างโค้ด

- `gptsalov/core.py`: config, Decimal, filters, indicators, signals, sizing
- `gptsalov/market.py`: public GET-only client, scanner, synthetic feed
- `gptsalov/paper.py`: persistent state machine, paper fills, risk locks, reporting
- `gptsalov/__main__.py`: CLI commands
- `tests/`: unit/integration/CLI tests ใช้ mock ไม่มี external network
- `VALIDATION.md`: ผลตรวจและสิ่งที่ยังไม่ยืนยัน
- `scripts/package_release.py`: สร้าง ZIP ที่ไม่รวมฐานข้อมูลหรือ secrets และทดสอบจาก ZIP หลังแตก

เอกสาร API ที่ใช้ตรวจรูปแบบ: [Binance USDⓈ-M Market Data](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data) — ตรวจ 9 กันยายน 2026 โค้ดใช้ public endpoints เท่านั้น ไม่ใช่ SDK สำหรับเทรดจริง
