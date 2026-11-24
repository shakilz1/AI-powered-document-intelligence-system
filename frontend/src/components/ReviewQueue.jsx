import React, { useEffect, useState } from "react";
import { api } from "../api.js";
import { ErrorBox, InvoiceTable } from "./common.jsx";

export default function ReviewQueue({ onOpen }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => {
    api.listInvoices({ needs_review: true, limit: 500 }).then(setData).catch(setError);
  }, []);
  return (
    <div>
      <h2>Invoices needing manual review {data ? `(${data.total})` : ""}</h2>
      <p className="muted">Flagged because of validation errors (missing fields, arithmetic mismatches, duplicates,
        cross-document differences) or uncertain/low-confidence extraction. Open one to inspect and correct it.</p>
      <ErrorBox error={error} />
      {data && <InvoiceTable items={data.items} onOpen={onOpen} />}
    </div>
  );
}
