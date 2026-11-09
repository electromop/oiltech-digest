import { enqueueDigestExport } from "../../api/digest";
import { downloadJobResult, getJob } from "../../api/jobs";

export type DigestExportFormat = "pdf" | "docx" | "html";

type ExportRequest = {
  month: string;
  limit: number;
  minScore: number;
  maxScore?: number;
  search?: string;
  topTag?: string;
};

// Выгрузка выпуска файлом: документ собирает на сервере фоновая задача, экран ждёт файл и сохраняет его.
// Номер задачи и прогресс наружу не показываются: для человека это просто ожидание документа.
export async function exportDigestFile(format: DigestExportFormat, request: ExportRequest): Promise<string> {
  const queued = await enqueueDigestExport(request.month, request.limit, request.minScore, format, {
    maxScore: request.maxScore,
    search: request.search,
    topTag: request.topTag,
  });
  const finished = await waitForExportJob(queued.job.id);
  if (finished.status !== "ok") {
    throw new Error(finished.error || "Не удалось подготовить документ");
  }
  const file = await downloadJobResult(finished.id);
  saveFile(file.blob, file.filename);
  return file.filename;
}

// Ждём готовности файла, опрашивая сервер раз в 2 секунды, до 160 секунд.
async function waitForExportJob(jobId: number) {
  for (let attempt = 0; attempt < 80; attempt += 1) {
    const job = await getJob(jobId);
    if (job.status === "ok" || job.status === "failed") {
      return job;
    }
    await sleep(2000);
  }
  throw new Error("Документ готовится дольше обычного. Попробуйте ещё раз чуть позже.");
}

function saveFile(blob: Blob, filename: string) {
  const url = window.URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.URL.revokeObjectURL(url);
}

function sleep(ms: number) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}
