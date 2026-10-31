import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { Alert, Button, ConfigProvider, DatePicker, Modal, Pagination, Radio, Select, Space, Tooltip, Upload } from "antd";
import zhCN from "antd/locale/zh_CN";
import dayjs from "dayjs";
import customParseFormat from "dayjs/plugin/customParseFormat";
import "dayjs/locale/zh-cn";

dayjs.extend(customParseFormat);
dayjs.locale("zh-cn");

const getPopupContainer = trigger => trigger.closest("dialog") || document.body;
const parseDate = value => value ? dayjs(value, "YYYY-MM-DD", true) : null;
const theme = { token: { colorPrimary: "#4a6be9", borderRadius: 7, controlHeight: 34, fontSize: 13,
  fontFamily: '-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif' } };

function useBridge(read, eventName) {
  const [value, setValue] = useState(read);
  useEffect(() => {
    const update = () => setValue(read());
    window.addEventListener(eventName, update);
    return () => window.removeEventListener(eventName, update);
  }, []);
  return value;
}

function useDomValues(ids) {
  const read = () => ids.map(id => document.getElementById(id)?.value || "");
  const [values, setValues] = useState(read);
  useEffect(() => {
    const sync = () => setValues(read());
    window.addEventListener("liveops:widgets-updated", sync);
    return () => window.removeEventListener("liveops:widgets-updated", sync);
  }, []);
  return values;
}

function DateWidget({ inputId }) {
  const [value] = useDomValues([inputId]);
  const input = document.getElementById(inputId);
  const minDate = parseDate(input?.min);
  const maxDate = parseDate(input?.max);
  return <DatePicker
    value={parseDate(value)}
    format="YYYY-MM-DD"
    minDate={minDate}
    maxDate={maxDate}
    allowClear={false}
    getPopupContainer={getPopupContainer}
    onChange={(_, dateString) => {
      input.value = dateString;
      input.dispatchEvent(new Event("change", { bubbles: true }));
    }}
  />;
}

function PaginationWidget({ keyName }) {
  const [props, setProps] = useState(() => window.liveopsGetPagination?.(keyName) || { current: 1, pageSize: 20, total: 0 });
  useEffect(() => {
    const update = event => {
      if (event.detail.key === keyName) setProps(event.detail);
    };
    window.addEventListener("liveops:pagination-updated", update);
    return () => window.removeEventListener("liveops:pagination-updated", update);
  }, [keyName]);
  return <Pagination
    current={props.current}
    pageSize={props.pageSize}
    total={props.total}
    showQuickJumper
    showSizeChanger
    responsive
    pageSizeOptions={[10, 20, 50, 100]}
    showTotal={total => `共 ${total} 条`}
    onChange={(page, pageSize) => window.liveopsPaginationChange(keyName, page, pageSize)}
  />;
}

const syncDescriptions = {
  incremental: "仅补充新增、发生变化或之前同步不完整的数据；已成功且未变化的数据会跳过，适合日常更新。",
  full: "把当前筛选日期范围内的全部主播汇总和小时明细重新写入飞书。按幂等键更新或补回记录，不清空其他日期和人工记录。",
};

function ReportTools() {
  const range = useBridge(() => window.liveopsGetReportRange(), "liveops:report-range-updated");
  const features = useBridge(() => window.liveopsGetFeatures(), "liveops:features-updated");
  const [selection, setSelection] = useState(null);
  const [mode, setMode] = useState("incremental");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  return <>
    <DatePicker.RangePicker
      value={range.map(parseDate)} format="YYYY-MM-DD" allowClear={false} disabled={!features.report_date_range}
      placeholder={["开始日期", "结束日期"]} getPopupContainer={getPopupContainer}
      presets={[{ label: "今天", value: [dayjs(), dayjs()] },
        { label: "近 7 天", value: [dayjs().subtract(6, "day"), dayjs()] },
        { label: "本月", value: [dayjs().startOf("month"), dayjs()] }]}
      onChange={(_, dates) => window.liveopsReportRangeChange(dates)}
    />
    <Tooltip title="同步当前筛选日期范围内的主播汇总及小时子记录，点击选择增量同步或全量刷新。">
      <Button type="primary" disabled={!features.bitable_monthly} onClick={() => { setSelection([...range]); setMode("incremental"); setError(""); }}>同步到飞书</Button>
    </Tooltip>
    {!features.report_date_range && <span className="panel-note">请重启直播运营后台加载范围筛选与月表功能。</span>}
    <Modal open={!!selection} destroyOnHidden title="同步所选日期的数据" okText="开始同步" cancelText="取消"
      confirmLoading={loading} closable={!loading} maskClosable={!loading}
      onCancel={() => { if (!loading) setSelection(null); }}
      onOk={async () => {
        setLoading(true); setError("");
        try { await window.liveopsSyncRange({ date_start: selection[0], date_end: selection[1], mode }); setSelection(null); }
        catch (exception) { setError(exception.message || String(exception)); }
        finally { setLoading(false); }
      }}>
      <p className="sync-range-label">日期范围：{selection?.join(" 至 ")}</p>
      <Radio.Group value={mode} onChange={event => setMode(event.target.value)} disabled={loading} className="sync-mode-options">
        <Space orientation="vertical" size={12}>
          {[["incremental", "增量同步"], ["full", "全量刷新"]].map(([value, label]) =>
            <Tooltip key={value} title={syncDescriptions[value]} placement="right" destroyOnHidden styles={{ root: { pointerEvents: "none" } }}>
              <Radio value={value}><strong>{label}</strong><span className="sync-mode-help">{syncDescriptions[value]}</span></Radio>
            </Tooltip>)}
        </Space>
      </Radio.Group>
      <Alert type="info" showIcon description="飞书保留当前月及前三个月的数据，各月写入对应月表；更早历史永久保存在本地。同步不重新采集或截图，进度可在采集记录查看。人工创建的表不会自动删除。" />
      {error && <Alert type="error" showIcon description={error} style={{ marginTop: 12 }} />}
    </Modal>
  </>;
}

const runKinds = [
  ["report", "主播排班任务"], ["shift_hour", "主播小时明细"], ["baseline", "千川累计基线"],
  ["interruption", "中断片段采集"], ["bitable_sync", "飞书范围同步"], ["hourly_sync", "小时明细同步"],
  ["screenshot_delivery", "截图补发"], ["hourly_screenshot_plan", "每小时截图计划"],
  ["anchor_screenshot_plan", "主播截图计划"], ["hourly_screenshot", "历史小时截图"], ["hourly_watch", "直播状态检查"],
].map(([value, label]) => ({ value, label }));
const runStatuses = [["queued", "队列中"], ["running", "执行中"], ["succeeded", "完成"],
  ["partial", "部分完成"], ["failed", "失败"], ["missed", "已漏拍"], ["skipped", "已跳过"]]
  .map(([value, label]) => ({ value, label }));

function RunFilters() {
  const filters = useBridge(() => window.liveopsGetRunFilters(), "liveops:run-filters-updated");
  const features = useBridge(() => window.liveopsGetFeatures(), "liveops:features-updated");
  const update = values => window.liveopsRunFiltersChange({ ...filters, ...values });
  return <Space wrap size={[10, 10]} className="run-filter-controls">
    <Select aria-label="时间字段" disabled={!features.run_filters} value={filters.timeField} style={{ width: 126 }}
      options={[{ value: "task", label: "任务时间" }, { value: "finished", label: "完成时间" }]}
      onChange={timeField => update({ timeField })} getPopupContainer={getPopupContainer} />
    <DatePicker.RangePicker showTime={{ format: "HH:mm:ss" }} format="YYYY-MM-DD HH:mm:ss"
      value={[dayjs(filters.start), dayjs(filters.end)]} allowClear={false} needConfirm disabled={!features.run_filters}
      className="run-time-range" placeholder={["开始时间", "结束时间"]}
      getPopupContainer={getPopupContainer}
      presets={[{ label: "今天", value: [dayjs().startOf("day"), dayjs().endOf("day")] },
        { label: "昨天", value: [dayjs().subtract(1, "day").startOf("day"), dayjs().subtract(1, "day").endOf("day")] },
        { label: "近 7 天", value: [dayjs().subtract(6, "day").startOf("day"), dayjs().endOf("day")] }]}
      onChange={(_, values) => update({ start: values[0], end: values[1] })} />
    <Select mode="multiple" disabled={!features.run_filters} aria-label="任务类型" placeholder="全部任务类型" options={runKinds}
      value={filters.kinds} onChange={kinds => update({ kinds })} allowClear maxTagCount="responsive"
      style={{ minWidth: 200 }} getPopupContainer={getPopupContainer} />
    <Select mode="multiple" disabled={!features.run_filters} aria-label="任务状态" placeholder="全部状态" options={runStatuses}
      value={filters.statuses} onChange={statuses => update({ statuses })} allowClear maxTagCount="responsive"
      style={{ minWidth: 165 }} getPopupContainer={getPopupContainer} />
    <Button disabled={!features.run_filters} onClick={() => window.liveopsResetRunFilters()}>重置筛选</Button>
  </Space>;
}

function RosterFileUpload() {
  const input = document.getElementById("importFile");
  const [files, setFiles] = useState([]);
  const [disabled, setDisabled] = useState(input.disabled);
  useEffect(() => {
    const reset = () => setFiles([]);
    window.addEventListener("liveops:roster-upload-reset", reset);
    const observer = new MutationObserver(() => setDisabled(input.disabled));
    observer.observe(input, { attributes: true, attributeFilter: ["disabled"] });
    return () => { observer.disconnect(); window.removeEventListener("liveops:roster-upload-reset", reset); };
  }, []);
  return <Upload accept=".xlsx,.csv" maxCount={1} fileList={files} disabled={disabled}
    beforeUpload={file => {
      const transfer = new DataTransfer(); transfer.items.add(file); input.files = transfer.files;
      input.dispatchEvent(new Event("change", { bubbles: true })); setFiles([file]); return false;
    }}
    onRemove={() => { input.value = ""; input.dispatchEvent(new Event("change", { bubbles: true })); setFiles([]); }}>
    <Button color="default" variant="filled" disabled={disabled} style={{ height: 40, fontWeight: 600 }}>选择排班文件</Button>
  </Upload>;
}

function mount(selector, component) {
  const node = document.querySelector(selector);
  if (node) createRoot(node).render(<ConfigProvider locale={zhCN} theme={theme}>{component}</ConfigProvider>);
}

for (const inputId of ["overview-date"]) {
  mount(`[data-antd-date="${inputId}"]`, <DateWidget inputId={inputId} />);
}
mount("#reports-pagination", <PaginationWidget keyName="reports" />);
mount("#runs-pagination", <PaginationWidget keyName="runs" />);
mount("#reports-tools", <ReportTools />);
mount("#runs-filters", <RunFilters />);
mount("#roster-file-upload", <RosterFileUpload />);
