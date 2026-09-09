# เริ่มรัน GPTsalov บน VPS

รุ่นนี้เป็น paper simulator เท่านั้น ไม่ต้องใช้ Binance API key คู่มือนี้ยังไม่ใช่หลักฐานว่าได้เชื่อมต่อหรือติดตั้งบน VPS แล้ว

## 1. เข้าสู่ VPS

ใช้ SSH username, private key/agent และ host fingerprint ที่ยืนยันจาก GCP ของคุณ หรือเปิด SSH-in-browser จากหน้า VM ใน Google Cloud Console ไม่ส่ง private key, token หรือรหัสผ่านลงแชตหรือ repository

ยังไม่ได้ใส่ IP/username ของเครื่องในโค้ด เพื่อให้ย้ายเครื่องได้โดยไม่แก้โปรแกรม

## 2. ตรวจสภาพแวดล้อม

รันบน VPS:

```bash
uname -s
python3 --version
git --version
docker --version
docker compose version
```

Python ต้อง 3.11 ขึ้นไป ถ้าใช้ Docker ไม่จำเป็นต้องติดตั้ง Python บน host ก่อน Docker ต้องมี Compose plugin และผู้ใช้ต้องมีสิทธิ์ใช้งาน Docker หากเครื่องยังขาด dependency ให้ตรวจ OS/version แล้วติดตั้งตามเอกสารของระบบนั้นก่อน คำสั่งด้านล่างไม่ได้ติดตั้งหรืออัปเกรดแพ็กเกจระบบเอง

## 3. Clone repository แบบ private

ตั้ง GitHub access ของ VPS ก่อน เช่น deploy key แบบ read-only ที่คุณเพิ่มให้ repository หรือวิธี Git authentication ที่องค์กรอนุญาต ห้ามฝัง token ลง clone URL

```bash
git clone git@github.com:wamalana/GPTsalov.git
cd GPTsalov
```

ถ้ามี checkout อยู่แล้ว อย่า clone ทับ ให้ตรวจ `git status` และเก็บการแก้ไขของคุณก่อน `git pull --ff-only`

## 4. ทดสอบก่อนรันต่อเนื่อง

หากใช้ Python บน host:

```bash
python3 -m unittest discover -s tests -v
python3 -m gptsalov check-access
python3 -m gptsalov scan --source binance --config config.toml
```

ถ้าใช้ Docker:

```bash
docker compose build
docker compose run --rm paper check-access
docker compose run --rm paper scan --source binance --config /app/config.toml
```

เมื่อเจอ HTTP block, rate limit, network error หรือข้อมูลผิดปกติ ให้หยุดตรวจสาเหตุก่อน ห้ามวนเปลี่ยน IP/region เพื่อเลี่ยงข้อจำกัด

## 5. เริ่ม paper process

หลังขั้นตอนตรวจ public data ผ่านแล้ว:

```bash
docker compose up -d
docker compose logs --tail 50 paper
docker compose exec paper python -m gptsalov status --db /data/paper.db --json
```

Docker ใช้ named volume เก็บประวัติ ไม่เปิดพอร์ตเข้าจากอินเทอร์เน็ต และไม่มีเว็บ dashboard ในรุ่นนี้

หากไม่ใช้ Docker รันใน terminal เพื่อทดสอบได้:

```bash
python3 -m gptsalov run --source binance --config config.toml --db data/binance-paper.db --cycles 0
```

คำสั่ง Python ใน terminal ไม่รับประกันว่าจะอยู่ต่อหลังปิด SSH ส่วน Docker รันเบื้องหลังได้ แต่ Compose ตั้ง `restart: "no"` เพื่อหยุดเมื่อเกิดข้อผิดพลาด จึงยังต้องเพิ่ม external monitoring และ supervised recovery ก่อนใช้งานแบบ production 24/7

## 6. ตรวจและหยุด

```bash
docker compose ps -a
docker compose logs --tail 100 paper
docker compose run --rm paper status --db /data/paper.db --json
docker compose stop paper
```

อ่าน `health`, `last_error`, `as_of_ms`, `data_age_ms`, `hard_lock` และ `daily_locked` ก่อนสรุปว่าระบบปกติ งาน status ไม่เชื่อมตลาดใหม่และไม่เปลี่ยนบัญชี

ห้ามลบ volume/DB เพื่อปลด risk lock ถ้าข้อมูลขาดขณะมีสถานะจำลอง ต้องตรวจและทำ reconciliation ซึ่งรุ่นนี้ยังไม่มีคำสั่ง unlock อัตโนมัติ

## สิ่งที่ยังต้องทำต่อ

- ทวนพฤติกรรมจาก public API จริงบน VPS และบันทึกผลตรวจ
- สำรอง SQLite อย่างสอดคล้องกับ WAL (ใช้ SQLite backup API หรือหยุด process ก่อนสำรองครบชุด)
- เพิ่ม watchdog และรายงานผ่านช่องทางที่มี authentication ก่อนตั้ง ChatGPT schedule
- เพิ่มข่าว/AI, historical funding, realistic backtest และ exchange demo execution ก่อนพิจารณาเงินจริง
