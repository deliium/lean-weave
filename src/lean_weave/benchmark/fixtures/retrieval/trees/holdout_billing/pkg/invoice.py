class Invoice:
    def total(self) -> int:
        return 0


def render_invoice(invoice: Invoice) -> str:
    return f"total={invoice.total()}"
