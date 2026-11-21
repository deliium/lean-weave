from pkg.invoice import render_invoice


def show_bill() -> str:
    from pkg.invoice import Invoice

    return render_invoice(Invoice())
