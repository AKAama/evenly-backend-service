from decimal import Decimal
from uuid import UUID
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.models import Expense, ExpenseSplit, LedgerMember, User, Settlement, ExpenseStatus


class SettlementCalculator:
    """Calculate settlements using greedy algorithm"""

    def __init__(self, db: Session, ledger_id: UUID):
        self.db = db
        self.ledger_id = ledger_id

    def get_confirmed_expenses(self) -> list[Expense]:
        """Get all confirmed expenses for the ledger"""
        return (
            self.db.query(Expense)
            .filter(
                Expense.ledger_id == self.ledger_id,
                Expense.status == ExpenseStatus.CONFIRMED
            )
            .all()
        )

    def get_ledger_members(self) -> list[LedgerMember]:
        """Get all members of the ledger"""
        return (
            self.db.query(LedgerMember)
            .filter(LedgerMember.ledger_id == self.ledger_id)
            .all()
        )

    def calculate_net_balances(self) -> dict[UUID, Decimal]:
        """
        Calculate net balance for each user.
        net = paid_amount - owed_amount

        Positive net: user should receive money
        Negative net: user owes money
        """
        members = self.get_ledger_members()
        member_ids = [m.user_id for m in members]

        # Step 1: Calculate owed amounts (from expense_splits)
        # Each user owes the sum of their splits
        owed_amounts = (
            self.db.query(ExpenseSplit.user_id, func.sum(ExpenseSplit.amount))
            .join(Expense)
            .filter(
                Expense.ledger_id == self.ledger_id,
                Expense.status == ExpenseStatus.CONFIRMED,
                ExpenseSplit.user_id.in_(member_ids)
            )
            .group_by(ExpenseSplit.user_id)
            .all()
        )

        owed_dict: dict[UUID, Decimal] = {uid: Decimal(str(amount or 0)) for uid, amount in owed_amounts}

        # Step 2: Calculate paid amounts (from expenses as payer)
        paid_amounts = (
            self.db.query(Expense.payer_id, func.sum(Expense.total_amount))
            .filter(
                Expense.ledger_id == self.ledger_id,
                Expense.status == ExpenseStatus.CONFIRMED,
                Expense.payer_id.in_(member_ids)
            )
            .group_by(Expense.payer_id)
            .all()
        )

        paid_dict: dict[UUID, Decimal] = {uid: Decimal(str(amount or 0)) for uid, amount in paid_amounts}

        # Step 3: Calculate net balances
        net_balances: dict[UUID, Decimal] = {}
        for member in members:
            owed = owed_dict.get(member.user_id, Decimal("0"))
            paid = paid_dict.get(member.user_id, Decimal("0"))
            net_balances[member.user_id] = paid - owed

        return net_balances

    def get_user_names(self) -> dict[UUID, str]:
        """Get display names for all ledger members"""
        members = self.get_ledger_members()
        user_ids = [m.user_id for m in members]
        users = self.db.query(User).filter(User.id.in_(user_ids)).all()
        return {u.id: u.display_name or u.email for u in users}

    def calculate_settlements(self) -> list[dict]:
        """
        Calculate optimal settlement instructions using greedy algorithm.

        Returns list of settlements:
        [
            {
                "from_user_id": UUID,
                "to_user_id": UUID,
                "amount": Decimal
            },
            ...
        ]
        """
        net_balances = self.calculate_net_balances()
        user_names = self.get_user_names()

        # Separate into creditors (positive balance) and debtors (negative balance)
        creditors: list[tuple[UUID, Decimal]] = []
        debtors: list[tuple[UUID, Decimal]] = []

        for user_id, balance in net_balances.items():
            if balance > 0:
                creditors.append((user_id, balance))
            elif balance < 0:
                debtors.append((user_id, -balance))  # Store as positive for easier calculation

        # Sort by amount (descending) for optimal settlement
        creditors.sort(key=lambda x: x[1], reverse=True)
        debtors.sort(key=lambda x: x[1], reverse=True)

        settlements: list[dict] = []

        # Greedy algorithm
        i, j = 0, 0
        while i < len(creditors) and j < len(debtors):
            creditor_id, creditor_amount = creditors[i]
            debtor_id, debtor_amount = debtors[j]

            # Transfer amount is minimum of what creditor is owed and what debtor owes
            transfer_amount = min(creditor_amount, debtor_amount)

            if transfer_amount > 0:
                settlements.append({
                    "from_user_id": debtor_id,
                    "from_user_name": user_names.get(debtor_id, "Unknown"),
                    "to_user_id": creditor_id,
                    "to_user_name": user_names.get(creditor_id, "Unknown"),
                    "amount": transfer_amount,
                })

            # Update balances
            creditors[i] = (creditor_id, creditor_amount - transfer_amount)
            debtors[j] = (debtor_id, debtor_amount - transfer_amount)

            # Move to next if settled
            if creditors[i][1] <= 0:
                i += 1
            if debtors[j][1] <= 0:
                j += 1

        return settlements


def create_settlement_record(
    db: Session,
    ledger_id: UUID,
    from_user_id: UUID,
    to_user_id: UUID,
    amount: Decimal,
    note: str | None = None
) -> Settlement:
    """Create a settlement record in the database"""
    settlement = Settlement(
        ledger_id=ledger_id,
        from_user_id=from_user_id,
        to_user_id=to_user_id,
        amount=amount,
        note=note,
    )
    db.add(settlement)
    db.commit()
    db.refresh(settlement)
    return settlement
