"""TNB Peninsular Malaysia domestic tariff (1 July 2025 - 31 December 2027).

Pure functions only (no I/O) so the bill maths can be unit tested.

Sources: Suruhanjaya Tenaga "Jadual Tarif Elektrik di Semenanjung Bagi Tempoh
1 Julai 2025 Hingga 31 Disember 2027" plus the community calculators this
add-on is modelled on. All rates are RM/kWh unless stated otherwise.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

TARIFF_GENERAL = "general"
TARIFF_TOU = "tou"

# Energy (generation) charge. Once monthly usage exceeds the threshold the
# higher rate applies to *all* kWh of the bill, not just the excess.
TIER_THRESHOLD_KWH = 1500.0
ENERGY_GENERAL = (0.2703, 0.3703)  # (<= 1500 kWh, > 1500 kWh)
ENERGY_TOU_PEAK = (0.2852, 0.3852)
ENERGY_TOU_OFFPEAK = (0.2443, 0.3443)

CAPACITY_RATE = 0.0455
NETWORK_RATE = 0.1285
RETAIL_CHARGE = 10.00  # RM/month, waived at or below the threshold
RETAIL_WAIVER_KWH = 600.0

AFA_THRESHOLD_KWH = 600.0  # AFA (monthly fuel adjustment) only above this usage
KWTBB_RATE = 0.016  # Kumpulan Wang Tenaga Boleh Baharu, above KWTBB_THRESHOLD_KWH
KWTBB_THRESHOLD_KWH = 300.0
SST_RATE = 0.08  # Service tax on the portion of usage above SST_THRESHOLD_KWH
SST_THRESHOLD_KWH = 600.0

# Energy Efficiency Incentive (EEI): a per-kWh rebate on the whole bill for
# usage up to 1000 kWh, sliding down as usage grows. (upper bound kWh, RM/kWh)
EEI_BANDS: tuple[tuple[float, float], ...] = (
    (200, 0.250),
    (250, 0.245),
    (300, 0.225),
    (350, 0.210),
    (400, 0.170),
    (450, 0.145),
    (500, 0.120),
    (550, 0.105),
    (600, 0.090),
    (650, 0.075),
    (700, 0.055),
    (750, 0.045),
    (800, 0.040),
    (850, 0.025),
    (900, 0.010),
    (1000, 0.005),
)


def eei_rate(kwh: float) -> float:
    """Return the EEI rebate rate (positive RM/kWh) for a monthly usage."""

    if kwh <= 0:
        return 0.0
    for upper, rate in EEI_BANDS:
        if kwh <= upper:
            return rate
    return 0.0


def energy_rates(tariff: str, total_kwh: float) -> tuple[float, float]:
    """Return the (peak, off-peak) energy rate for the bill's tier.

    For the general tariff both values are the same flat rate.
    """

    tier = 1 if total_kwh > TIER_THRESHOLD_KWH else 0
    if tariff == TARIFF_TOU:
        return ENERGY_TOU_PEAK[tier], ENERGY_TOU_OFFPEAK[tier]
    return ENERGY_GENERAL[tier], ENERGY_GENERAL[tier]


@dataclass(slots=True)
class Bill:
    """A fully itemised monthly bill estimate (RM unless noted)."""

    tariff: str
    import_kwh: float
    peak_kwh: float
    offpeak_kwh: float
    export_kwh: float
    tier: int
    energy: float
    capacity: float
    network: float
    retail: float
    afa: float
    eei: float
    nem_credit: float
    nem_excess_kwh: float
    kwtbb: float
    sst: float
    total: float

    def as_dict(self) -> dict[str, float | int | str]:
        """Return the bill as a JSON-friendly dict with rounded money values."""

        data = asdict(self)
        for key, value in data.items():
            if isinstance(value, float):
                data[key] = round(value, 3 if key.endswith("_kwh") else 2)
        return data


def calculate(  # noqa: PLR0913 - itemised inputs, the extras are keyword-only
    peak_kwh: float,
    offpeak_kwh: float,
    export_kwh: float = 0.0,
    *,
    tariff: str = TARIFF_GENERAL,
    afa_rate: float = 0.0,
    nem_carry_kwh: float = 0.0,
) -> Bill:
    """Estimate a monthly bill.

    For the general tariff pass all usage as ``peak_kwh`` (or split it any way:
    both halves use the same rate). ``export_kwh`` plus ``nem_carry_kwh``
    (credit carried forward from previous months) is credited NEM-style at the
    energy + capacity + network rates, capped at the import charges; any
    unused credit is returned as ``nem_excess_kwh``.
    """

    peak_kwh = max(peak_kwh, 0.0)
    offpeak_kwh = max(offpeak_kwh, 0.0)
    total_kwh = peak_kwh + offpeak_kwh
    tier = 2 if total_kwh > TIER_THRESHOLD_KWH else 1

    peak_rate, offpeak_rate = energy_rates(tariff, total_kwh)
    energy = peak_kwh * peak_rate + offpeak_kwh * offpeak_rate
    capacity = total_kwh * CAPACITY_RATE
    network = total_kwh * NETWORK_RATE
    retail = RETAIL_CHARGE if total_kwh > RETAIL_WAIVER_KWH else 0.0
    afa = total_kwh * afa_rate if total_kwh > AFA_THRESHOLD_KWH else 0.0
    eei = -total_kwh * eei_rate(total_kwh)

    # NEM: exported kWh offset the import at energy + capacity + network.
    credit_kwh = max(export_kwh, 0.0) + max(nem_carry_kwh, 0.0)
    nem_credit = 0.0
    nem_excess_kwh = 0.0
    if credit_kwh > 0:
        # Off-peak energy rate for ToU exports is the conservative choice.
        credit_rate = offpeak_rate + CAPACITY_RATE + NETWORK_RATE
        import_base = energy + capacity + network
        uncapped = credit_kwh * credit_rate
        nem_credit = -min(uncapped, import_base)
        if uncapped > import_base and credit_rate > 0:
            nem_excess_kwh = (uncapped - import_base) / credit_rate

    usage_charges = max(energy + capacity + network + afa + eei + nem_credit, 0.0)
    kwtbb = usage_charges * KWTBB_RATE if total_kwh > KWTBB_THRESHOLD_KWH else 0.0
    sst = 0.0
    if total_kwh > SST_THRESHOLD_KWH:
        taxable_share = (total_kwh - SST_THRESHOLD_KWH) / total_kwh
        sst = (usage_charges * taxable_share + retail) * SST_RATE

    total = usage_charges + retail + kwtbb + sst
    return Bill(
        tariff=tariff,
        import_kwh=total_kwh,
        peak_kwh=peak_kwh,
        offpeak_kwh=offpeak_kwh,
        export_kwh=max(export_kwh, 0.0),
        tier=tier,
        energy=energy,
        capacity=capacity,
        network=network,
        retail=retail,
        afa=afa,
        eei=eei,
        nem_credit=nem_credit,
        nem_excess_kwh=nem_excess_kwh,
        kwtbb=kwtbb,
        sst=sst,
        total=round(total, 2),
    )


def marginal_rate(tariff: str, total_kwh: float, *, peak: bool, afa_rate: float = 0.0) -> float:
    """Approximate RM cost of the next kWh at the current usage level."""

    peak_rate, offpeak_rate = energy_rates(tariff, total_kwh)
    rate = (peak_rate if peak else offpeak_rate) + CAPACITY_RATE + NETWORK_RATE
    if total_kwh > AFA_THRESHOLD_KWH:
        rate += afa_rate
    rate -= eei_rate(total_kwh)
    if total_kwh > KWTBB_THRESHOLD_KWH:
        rate *= 1 + KWTBB_RATE
    if total_kwh > SST_THRESHOLD_KWH:
        rate *= 1 + SST_RATE
    return round(rate, 4)
