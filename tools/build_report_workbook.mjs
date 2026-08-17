import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";


function parseCsv(text) {
  const rows = [];
  let row = [];
  let field = "";
  let quoted = false;
  const source = text.replace(/^\uFEFF/, "");
  for (let index = 0; index < source.length; index += 1) {
    const char = source[index];
    if (quoted) {
      if (char === '"' && source[index + 1] === '"') {
        field += '"';
        index += 1;
      } else if (char === '"') {
        quoted = false;
      } else {
        field += char;
      }
    } else if (char === '"') {
      quoted = true;
    } else if (char === ",") {
      row.push(field);
      field = "";
    } else if (char === "\n") {
      row.push(field.replace(/\r$/, ""));
      rows.push(row);
      row = [];
      field = "";
    } else {
      field += char;
    }
  }
  if (field || row.length) {
    row.push(field.replace(/\r$/, ""));
    rows.push(row);
  }
  return rows.filter((item) => item.some((value) => value !== ""));
}


function columnLetter(index) {
  let value = index + 1;
  let result = "";
  while (value > 0) {
    value -= 1;
    result = String.fromCharCode(65 + (value % 26)) + result;
    value = Math.floor(value / 26);
  }
  return result;
}


async function readRows(filePath, fallbackHeaders) {
  try {
    const text = await fs.readFile(filePath, "utf8");
    const rows = parseCsv(text);
    return rows.length ? rows : [fallbackHeaders];
  } catch (error) {
    if (error?.code === "ENOENT") return [fallbackHeaders];
    throw error;
  }
}


function configureSheet(workbook, config, rows) {
  const sheet = workbook.worksheets.add(config.sheetName);
  const columnCount = Math.max(...rows.map((row) => row.length), 1);
  const padded = rows.map((row) => [
    ...row,
    ...Array(Math.max(0, columnCount - row.length)).fill(""),
  ]);
  const used = sheet.getRangeByIndexes(0, 0, padded.length, columnCount);
  used.values = padded;
  used.format = {
    font: { name: "Aptos", size: 10, color: "#172033" },
    verticalAlignment: "top",
  };
  const header = sheet.getRangeByIndexes(0, 0, 1, columnCount);
  header.format = {
    fill: "#16324F",
    font: { name: "Aptos Display", size: 10, bold: true, color: "#FFFFFF" },
    verticalAlignment: "center",
    wrapText: true,
    rowHeight: 30,
    borders: { preset: "outside", style: "thin", color: "#16324F" },
  };
  sheet.freezePanes.freezeRows(1);
  sheet.showGridLines = false;

  const endCell = `${columnLetter(columnCount - 1)}${padded.length}`;
  const table = sheet.tables.add(`A1:${endCell}`, true, config.tableName);
  table.style = "TableStyleMedium2";
  table.showFilterButton = true;
  table.showBandedRows = true;

  const headers = padded[0];
  for (let index = 0; index < columnCount; index += 1) {
    const name = headers[index] || "";
    const range = sheet.getRangeByIndexes(0, index, padded.length, 1);
    let width = 18;
    if (["url", "source"].includes(name)) width = 46;
    if (["context", "review_reason", "analyst_next_step", "description"].includes(name)) width = 54;
    if (["detected_types", "type_counts", "query"].includes(name)) width = 34;
    if (["status", "review_priority", "highest_severity"].includes(name)) width = 20;
    if (["detection_count", "sensitive_candidate_count", "js_checked"].includes(name)) width = 14;
    range.format.columnWidth = width;
    if (width >= 34) range.format.wrapText = true;
    if (name.endsWith("_utc") && padded.length > 1) {
      sheet.getRangeByIndexes(1, index, padded.length - 1, 1).format.numberFormat =
        "yyyy-mm-dd hh:mm:ss";
      range.format.columnWidth = 21;
    }
  }
  if (padded.length > 1) {
    sheet.getRangeByIndexes(1, 0, padded.length - 1, columnCount).format.rowHeight = 32;
  }

  const priorityIndex = headers.indexOf("review_priority");
  if (priorityIndex >= 0 && padded.length > 1) {
    const priorityRange = sheet.getRangeByIndexes(1, priorityIndex, padded.length - 1, 1);
    priorityRange.conditionalFormats.add("containsText", {
      text: "HIGH", format: { fill: "#FECACA", font: { bold: true, color: "#991B1B" } },
    });
    priorityRange.conditionalFormats.add("containsText", {
      text: "MEDIUM", format: { fill: "#FEF3C7", font: { bold: true, color: "#92400E" } },
    });
    priorityRange.conditionalFormats.add("containsText", {
      text: "LOW", format: { fill: "#DBEAFE", font: { color: "#1E40AF" } },
    });
  }
  return sheet;
}


const rootDir = process.cwd();
const outputPath = path.resolve(process.argv[2] || "output/supabase_scan_report.xlsx");
const previewDir = path.resolve(process.argv[3] || ".artifact-work/previews");
const configs = [
  {
    sheetName: "검토 후보", tableName: "ReviewCandidates",
    source: "output/report_candidates.csv",
    headers: ["url", "review_priority", "status", "detected_types", "review_reason"],
  },
  {
    sheetName: "전체 요약", tableName: "BatchSummary",
    source: "output/batch_summary.csv",
    headers: ["url", "status", "review_priority"],
  },
  {
    sheetName: "상세 탐지", tableName: "DetailedFindings",
    source: "output/results.csv",
    headers: ["url", "source", "type", "masked_value", "context"],
  },
  {
    sheetName: "발견 URL", tableName: "DiscoveryResults",
    source: "output/discovery_results.csv",
    headers: ["discovered_utc", "query", "search_offset", "url", "domain"],
  },
  {
    sheetName: "API 요청", tableName: "ApiRequests",
    source: "output/search_request_log.csv",
    headers: ["requested_utc", "query", "search_offset", "request_mode", "status", "result_count", "error_type"],
  },
];

const workbook = Workbook.create();
for (const config of configs) {
  const rows = await readRows(path.resolve(rootDir, config.source), config.headers);
  configureSheet(workbook, config, rows);
}

await fs.mkdir(path.dirname(outputPath), { recursive: true });
await fs.mkdir(previewDir, { recursive: true });
for (const config of configs) {
  const preview = await workbook.render({
    sheetName: config.sheetName,
    range: "A1:H18",
    scale: 1,
    format: "png",
  });
  const bytes = new Uint8Array(await preview.arrayBuffer());
  await fs.writeFile(path.join(previewDir, `${config.tableName}.png`), bytes);
}

const structureCheck = await workbook.inspect({
  kind: "workbook,sheet,table",
  maxChars: 5000,
  tableMaxRows: 3,
  tableMaxCols: 8,
});
console.log(structureCheck.ndjson);
const formulaErrors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 100 },
  summary: "final formula error scan",
});
console.log(formulaErrors.ndjson);

const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);
console.log(JSON.stringify({ outputPath, sheets: configs.map((item) => item.sheetName) }));
