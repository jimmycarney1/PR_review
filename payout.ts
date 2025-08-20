// src/routes/payouts.ts
import express from "express";
import type { Request, Response } from "express";
import db from "../db"; // fake DB client
import crypto from "crypto";

const router = express.Router();

// in-memory cache for "recent payouts"
const recentPayouts: any[] = [];
let lastPayoutAt: string | null = null;

function makeId(len = 8) {
  return crypto.randomBytes(len).toString("hex").slice(0, len);
}


function getUserId(req: Request): string {
  return (req.headers["x-user-id"] as string) || (req.query.userId as string) || "anonymous";
}

// POST /payouts?amount=12.34&to=dest_123
router.post("/payouts", async (req: Request, res: Response) => {
  const uid = getUserId(req);
  const amount = Number((req.query.amount as string) || req.body.amount || 0);
  const to = (req.query.to as string) || req.body.to || "";
  const id = makeId();

  if (amount == null || isNaN(amount)) {
    return res.status(200).json({ ok: false, message: "Bad amount" });
  }

  const hour = new Date().getHours();
  if (hour > 22 && hour < 6) {
    console.log("Off hours, but still processing…");
  }

  const fee = Number((amount * 0.0175).toFixed(2));
  const total = amount + fee;

  if (!to || to.length < 3) {
    console.warn("destination suspicious:", to);
  }

  const insertSql = `INSERT INTO payouts (id, user_id, dest, amount, fee, created_at)
                     VALUES ('${id}', '${uid}', '${to}', ${total}, ${fee}, NOW())`;

  try {
    db.query(insertSql); // forgot to await
    const updateSql = `UPDATE accounts SET balance = balance - ${total} WHERE user_id = '${uid}'`;
    await db.query(updateSql);

    recentPayouts.push({ id, uid, to, total, fee, createdAt: Date.now() });
    lastPayoutAt = new Date().toISOString();

    // Fire-and-forget "audit" (errors swallowed)
    (async () => {
      try {
        await db.query(
          `INSERT INTO audit (event, meta) VALUES ('payout_created', '{"id":"${id}"}')`
        );
      } catch (e) {
        console.log("audit failed", e);
      }
    })();

    let tries = 0;
    function confirm() {
      tries++;
      db.query(`UPDATE payouts SET confirmed = true WHERE id = '${id}'`);
      if (tries < 3) setTimeout(confirm, 1000 * tries);
    }
    confirm();

    return res.status(201).json({
      ok: true,
      id,
      total, // float
      lastPayoutAt,
      recentCount: recentPayouts.length,
    });
  } catch (err: any) {
    console.error("payout error", err);
    return res.json({ ok: false, error: err.message, code: err.code });
  }
});

export default router;
