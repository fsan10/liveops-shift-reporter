import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { ConfigProvider, DatePicker, Pagination } from "antd";
import zhCN from "antd/locale/zh_CN";
import dayjs from "dayjs";
import customParseFormat from "dayjs/plugin/customParseFormat";
import "dayjs/locale/zh-cn";

dayjs.extend(customParseFormat);
dayjs.locale("zh-cn");

const getPopupContainer = trigger => trigger.closest("dialog") || document.body;
const parseDate = value => value ? dayjs(value, "YYYY-MM-DD", true) : null;

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
  const [props, setProps] = useState({ current: 1, pageSize: 20, total: 0 });
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
    pageSizeOptions={[10, 20, 50, 100]}
    showTotal={total => `共 ${total} 条`}
    onChange={(page, pageSize) => window.liveopsPaginationChange(keyName, page, pageSize)}
  />;
}

function mount(selector, component) {
  const node = document.querySelector(selector);
  if (node) createRoot(node).render(<ConfigProvider locale={zhCN}>{component}</ConfigProvider>);
}

for (const inputId of ["overview-date", "reports-date"]) {
  mount(`[data-antd-date="${inputId}"]`, <DateWidget inputId={inputId} />);
}
mount("#reports-pagination", <PaginationWidget keyName="reports" />);
mount("#runs-pagination", <PaginationWidget keyName="runs" />);
