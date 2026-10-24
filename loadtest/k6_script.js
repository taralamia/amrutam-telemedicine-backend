import http from "k6/http";
import { check } from "k6";
import { Counter, Rate, Trend } from "k6/metrics";
import exec from "k6/execution";
import { SharedArray } from "k6/data";

const loadData = new SharedArray("loadtest-data", () => [JSON.parse(open("./tokens.json"))])[0];
const readRate = positiveInteger(__ENV.READS_PER_SECOND, 150, "READS_PER_SECOND");
const writeRate = positiveInteger(__ENV.WRITES_PER_SECOND, 10, "WRITES_PER_SECOND");
const duration = __ENV.DURATION || "2m";
const baseUrl = (__ENV.BASE_URL || loadData.base_url).replace(/\/$/, "");
const unexpectedErrors = new Rate("unexpected_error_rate");
const readLatency = new Trend("read_latency", true);
const writeLatency = new Trend("write_latency", true);
const contentionSuccesses = new Counter("contention_successes");
const contentionConflicts = new Counter("contention_conflicts");

function positiveInteger(value, fallback, name) {
  const parsed = value === undefined ? fallback : Number(value);
  if (!Number.isInteger(parsed) || parsed <= 0) {
    throw new Error(`${name} must be a positive integer`);
  }
  return parsed;
}

export const options = {
  scenarios: {
    reads: {
      executor: "constant-arrival-rate",
      exec: "readDoctors",
      rate: readRate,
      timeUnit: "1s",
      duration,
      preAllocatedVUs: Math.max(50, readRate * 2),
      maxVUs: Math.max(500, readRate * 10),
    },
    writes: {
      executor: "constant-arrival-rate",
      exec: "writeConsultation",
      rate: writeRate,
      timeUnit: "1s",
      duration,
      preAllocatedVUs: Math.max(20, writeRate * 2),
      maxVUs: Math.max(200, writeRate * 10),
    },
    contention: {
      executor: "per-vu-iterations",
      exec: "contendForSlot",
      vus: 100,
      iterations: 1,
      maxDuration: "30s",
    },
  },
  thresholds: {
    read_latency: ["p(95)<200"],
    write_latency: ["p(95)<500"],
    unexpected_error_rate: ["rate<0.01"],
    dropped_iterations: ["count==0"],
    contention_successes: ["count==1"],
    contention_conflicts: ["count==99"],
  },
};

function patientFor(index) {
  const patient = loadData.patients[index % loadData.patients.length];
  return {
    Authorization: `Bearer ${patient.token}`,
    "Content-Type": "application/json",
  };
}

function recordUnexpected(isUnexpected) {
  unexpectedErrors.add(isUnexpected ? 1 : 0);
}

export function readDoctors() {
  const index = exec.scenario.iterationInTest;
  const specialties = ["General Medicine", "Dermatology", "Cardiology", "Pediatrics", "Orthopedics"];
  const languages = ["English", "Hindi", "Bengali", "Tamil", "Marathi"];
  const sortBy = ["rating", "fee", "availability"];
  const query = [
    `specialty=${encodeURIComponent(specialties[index % specialties.length])}`,
    `language=${encodeURIComponent(languages[index % languages.length])}`,
    `available_only=${index % 2 === 0}`,
    `sort_by=${sortBy[index % sortBy.length]}`,
  ].join("&");
  const response = http.get(`${baseUrl}/v1/doctors?${query}`, { tags: { name: "GET /v1/doctors" } });
  const success = check(response, {
    "doctor search returns 200": (res) => res.status === 200,
  });
  readLatency.add(response.timings.duration);
  recordUnexpected(!success);
}

export function writeConsultation() {
  const index = exec.scenario.iterationInTest;
  const slotId = loadData.slot_ids[index];
  if (!slotId) {
    recordUnexpected(true);
    check(false, { "seed contains a distinct slot for this write": (value) => value });
    return;
  }
  const headers = patientFor(index);
  headers["Idempotency-Key"] = `loadtest-write-${index}-${__VU}`;
  const response = http.post(
    `${baseUrl}/v1/consultations`,
    JSON.stringify({ availability_slot_id: slotId }),
    { headers, tags: { name: "POST /v1/consultations" } },
  );
  const success = check(response, {
    "consultation booking returns 201": (res) => res.status === 201,
  });
  writeLatency.add(response.timings.duration);
  recordUnexpected(!success);
}

export function contendForSlot() {
  const headers = patientFor(__VU - 1);
  headers["Idempotency-Key"] = `loadtest-contention-${__VU}`;
  const response = http.post(
    `${baseUrl}/v1/consultations`,
    JSON.stringify({ availability_slot_id: loadData.contention_slot_id }),
    { headers, tags: { name: "POST /v1/consultations contention" } },
  );
  const accepted = response.status === 201 || response.status === 409;
  check(response, {
    "contention returns exactly the allowed status codes": (res) =>
      res.status === 201 || res.status === 409,
  });
  if (response.status === 201) {
    contentionSuccesses.add(1);
  } else if (response.status === 409) {
    contentionConflicts.add(1);
  }
  recordUnexpected(!accepted);
}
