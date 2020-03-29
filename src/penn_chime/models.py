"""Models.

Changes affecting results or their presentation should also update
parameters.py `change_date`, so users can see when results have last
changed
"""

from __future__ import annotations

from datetime import date, datetime
from logging import INFO, basicConfig, getLogger
from sys import stdout
from typing import Dict, Generator, Tuple, Optional

import numpy as np  # type: ignore
import pandas as pd  # type: ignore

from .constants import EPSILON, CHANGE_DATE
from .parameters import Parameters


basicConfig(
    level=INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=stdout,
)
logger = getLogger(__name__)


class SimSirModel:

    def __init__(self, p: Parameters):

        n_days_since = None
        if p.date_first_hospitalized:
            n_days_since = (p.current_date - p.date_first_hospitalized).days
            logger.debug(
                "%s: %s - %s = %s days",
                datetime.now(),
                p.current_date, p.date_first_hospitalized,
                n_days_since)
        self.n_days_since = n_days_since

        self.rates = {
            key: d.rate
            for key, d in p.dispositions.items()
        }

        self.lengths_of_stay = {
            key: d.length_of_stay
            for key, d in p.dispositions.items()
        }

        # Note: this should not be an integer.
        # We're appoximating infected from what we do know.
        # TODO market_share > 0, hosp_rate > 0
        infected = (
            p.current_hospitalized / p.market_share / p.hospitalized.rate
        )

        susceptible = p.population - infected

        detection_probability = (
            p.known_infected / infected if infected > EPSILON else None
        )

        intrinsic_growth_rate = get_growth_rate(p.doubling_time)

        gamma = 1.0 / p.infectious_days

        # Contact rate, beta
        beta = (
            (intrinsic_growth_rate + gamma)
            / susceptible
            * (1.0 - p.relative_contact_rate)
        )  # {rate based on doubling time} / {initial susceptible}

        # r_t is r_0 after distancing
        r_t = beta / gamma * susceptible

        # Simplify equation to avoid division by zero:
        # self.r_naught = r_t / (1.0 - relative_contact_rate)
        r_naught = (intrinsic_growth_rate + gamma) / gamma
        doubling_time_t = 1.0 / np.log2(
            beta * susceptible - gamma + 1)

        self.susceptible = susceptible
        self.infected = infected
        self.recovered = p.recovered

        self.detection_probability = detection_probability
        self.doubling_time_t = doubling_time_t
        self.beta = beta
        self.gamma = gamma
        self.intrinsic_growth_rate = intrinsic_growth_rate
        self.r_t = r_t
        self.r_naught = r_naught
        self.infected = 1.0 / p.hospitalized.rate / p.market_share

        if p.date_first_hospitalized is None and p.doubling_time is not None:
            logger.info('Using doubling_time.')
            self.i_day = 0
            self.beta = (
                (intrinsic_growth_rate + gamma)
                / susceptible
            )

            self.run_projection(p)
            self.i_day = i_day = int(get_argmin_ds(self.census_df, p.current_hospitalized))
            self.run_projection(p)
            self.infected = self.raw_df['infected'].values[i_day]
            self.susceptible = self.raw_df['susceptible'].values[i_day]
            self.recovered = self.raw_df['recovered'].values[i_day]
            self.n_days_since = i_day
            logger.info('Set i_day = %s', i_day)

        elif p.date_first_hospitalized is not None and p.doubling_time is None:
            logger.info('Using date_first_hospitalized.')
            self.i_day = self.n_days_since
            min_loss = 2.0**99
            dts = np.linspace(1, 15, 29)
            losses = np.zeros(dts.shape[0])
            self.current_hospitalized = p.current_hospitalized
            for i, i_dt in enumerate(dts):
                intrinsic_growth_rate = get_growth_rate(i_dt)
                self.beta = get_beta(intrinsic_growth_rate, self.gamma, self.susceptible, 0.0)

                self.run_projection(p)
                loss = self.get_loss()
                losses[i] = loss

            p.doubling_time = doubling_time = dts[pd.Series(losses).argmin()]
            logger.info('Set doubling_time = %s', doubling_time)
            intrinsic_growth_rate = get_growth_rate(p.doubling_time)
            self.beta = get_beta(intrinsic_growth_rate, self.gamma, self.susceptible, 0.0)
            self.run_projection(p)

            self.intrinsic_growth_rate = intrinsic_growth_rate
            self.beta = beta
            self.doubling_time_t = doubling_time_t
            self.population = p.population
            self.r_t = r_t
            self.r_naught = r_naught
        else:
            raise AssertionError('doubling_time or date_first_hospitalized must be provided.')


        self.sim_sir_w_date_df = build_sim_sir_w_date_df(self.raw_df, p.current_date)

        self.daily_growth_rate = get_growth_rate(p.doubling_time)
        self.daily_growth_rate_t = get_growth_rate(self.doubling_time_t)

    def run_projection(self, p):
        self.raw_df = sim_sir_df(
            self.susceptible,
            self.infected,
            p.recovered,
            self.beta,
            self.gamma,
            p.n_days + self.i_day,
            -self.i_day
        )
        self.dispositions_df = build_dispositions_df(self.raw_df, self.rates, p.market_share, p.current_date)
        self.admits_df = build_admits_df(self.dispositions_df)
        self.census_df = build_census_df(self.admits_df, self.lengths_of_stay)
        self.current_infected = self.raw_df.infected.loc[self.i_day]

    def get_loss(self) -> float:
        """Squared error: predicted vs. actual current hospitalized."""
        predicted = self.census_df.hospitalized.loc[self.n_days_since]
        return (self.current_hospitalized - predicted) ** 2.0


def get_argmin_ds(census_df: pd.DataFrame, current_hospitalized: float) -> float:
    losses_df = (census_df.hospitalized - current_hospitalized) ** 2.0
    return losses_df.argmin()


def get_beta(
    intrinsic_growth_rate: float,
    gamma: float,
    susceptible: float,
    relative_contact_rate: float
) -> float:
    return (
        (intrinsic_growth_rate + gamma)
        / susceptible
        * (1.0 - relative_contact_rate)
    )


def get_growth_rate(doubling_time: Optional[float]) -> float:
    """Calculates average daily growth rate from doubling time."""
    if doubling_time is None or doubling_time == 0.0:
        return 0.0
    return (2.0 ** (1.0 / doubling_time) - 1.0)


def get_loss(census_df: pd.DataFrame, current_hospitalized: float, n_days_since: int) -> float:
    """Squared error: predicted vs. actual current hospitalized."""
    predicted = census_df.hospitalized.loc[n_days_since]
    return (current_hospitalized - predicted) ** 2.0


def sir(
    s: float, i: float, r: float, beta: float, gamma: float, n: float
) -> Tuple[float, float, float]:
    """The SIR model, one time step."""
    s_n = (-beta * s * i) + s
    i_n = (beta * s * i - gamma * i) + i
    r_n = gamma * i + r

    # TODO:
    #   Post check dfs for negative values and
    #   warn the user that their input data is bad.
    #   JL: I suspect that these adjustments covered bugs.

    #if s_n < 0.0:
    #    s_n = 0.0
    #if i_n < 0.0:
    #    i_n = 0.0
    #if r_n < 0.0:
    #    r_n = 0.0
    scale = n / (s_n + i_n + r_n)
    return s_n * scale, i_n * scale, r_n * scale


def gen_sir(
    s: float, i: float, r: float,
    beta: float, gamma: float, n_days: int, i_day: int = 0
) -> Generator[Tuple[int, float, float, float], None, None]:
    """Simulate SIR model forward in time yielding tuples."""
    s, i, r = (float(v) for v in (s, i, r))
    n = s + i + r
    d = i_day
    # TODO: Ask corey if n_days is really required for the sim
    # while i >= 0.5:
    for _ in range(n_days):
        yield d, s, i, r
        s, i, r = sir(s, i, r, beta, gamma, n)
        d += 1
    yield d, s, i, r


def sim_sir_df(
    s: float, i: float, r: float, beta: float, gamma: float, n_days: int, i_day: int = 0
) -> pd.DataFrame:
    """Simulate the SIR model forward in time."""
    return pd.DataFrame(
        data=gen_sir(s, i, r, beta, gamma, n_days, i_day),
        columns=("day", "susceptible", "infected", "recovered"),
    )


def build_sim_sir_w_date_df(
    raw_df: pd.DataFrame,
    current_date: datetime,
) -> pd.DataFrame:
    day = raw_df.day
    return pd.DataFrame({
        "day": day,
        "date": day.astype('timedelta64[D]') + np.datetime64(current_date),
        "susceptible": raw_df.susceptible,
        "infected": raw_df.infected,
        "recovered": raw_df.recovered,
    })


def build_dispositions_df(
    raw_df: pd.DataFrame,
    rates: Dict[str, float],
    market_share: float,
    current_date: datetime,
) -> pd.DataFrame:
    """Build dispositions dataframe of patients adjusted by rate and market_share."""
    patients = raw_df.infected + raw_df.recovered
    day = raw_df.day
    return pd.DataFrame({
        "day": day,
        "date": day.astype('timedelta64[D]') + np.datetime64(current_date),
        **{
            key: patients * rate * market_share
            for key, rate in rates.items()
        }
    })


def build_admits_df(dispositions_df: pd.DataFrame) -> pd.DataFrame:
    """Build admits dataframe from dispositions."""
    admits_df = dispositions_df.iloc[:-1, :] - dispositions_df.shift(1)
    admits_df.day = dispositions_df.day
    admits_df.date = dispositions_df.date
    return admits_df


def build_census_df(
    admits_df: pd.DataFrame,
    lengths_of_stay: Dict[str, int],
) -> pd.DataFrame:
    """Average Length of Stay for each disposition of COVID-19 case (total guesses)"""
    return pd.DataFrame({
        'day': admits_df.day,
        'date': admits_df.date,
        **{
            key: (
                admits_df[key].cumsum().iloc[:-los]
                - admits_df[key].cumsum().shift(los).fillna(0)
            ).apply(np.ceil)
            for key, los in lengths_of_stay.items()
        }
    })
