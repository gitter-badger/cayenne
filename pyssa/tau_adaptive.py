"""
    Implementation of the tau leaping algorithm in Numba
"""

from typing import Tuple
from numba import njit
import numpy as np
from .utils import get_kstoc, roulette_selection
from .direct_naive import direct_naive

HIGH = 1e20


def get_HOR(react_stoic: np.ndarray):
    """ Determine the HOR vector. HOR(i) is the highest order of reaction
        in which species S_i appears as a reactant.

        Parameters
        ----------
        react_stoic : (ns, nr) ndarray
            A 2D array of the stoichiometric coefficients of the reactants.
            Reactions are rows and species are columns.

        Returns
        -------
        HOR : np.ndarray
            Highest order of the reaction for the reactive species as
            defined under Eqn. (27) of [1]_. HOR can be 1, 2 or 3
            if the species appears only once in the reactants.
            If HOR is -2, it appears twice in a second order reaction.
            If HOR is -3, it appears thrice in a third order reaction.
            If HOR is -32, it appears twice in a third order reaction.
            The corresponding value of `g_i` in Eqn. (27) is handled
            by `tau_adaptive`.

        References
        ----------
        .. [1] Cao, Y., Gillespie, D.T., Petzold, L.R., 2006.
        Efficient step size selection for the tau-leaping simulation
        method. J. Chem. Phys. 124, 044109. doi:10.1063/1.2159468
    """
    ns = react_stoic.shape[0]
    HOR = np.zeros([ns])
    orders = np.sum(react_stoic, axis=0)
    for ind in range(ns):
        this_orders = orders[np.where(react_stoic[ind, :] > 0)]
        if len(this_orders) == 0:
            HOR[ind] = 0
            continue
        HOR[ind] = np.max(this_orders)
        if HOR[ind] == 1:
            continue
        order_2_indices = np.where(orders == 2)
        if order_2_indices[0].size > 0:
            if np.max(react_stoic[ind, np.where(orders == 2)]) == 2 and HOR[ind] == 2:
                HOR[ind] = -2  # g_i should be (2 + 1/(x_i-1))
        if np.where(orders == 3):
            if (
                HOR[ind] == 3
                and np.max(react_stoic[ind, np.where(this_orders == 3)]) == 2
            ):
                HOR[ind] = -32  # g_i should be (3/2 * (2 + 1/(x_i-1)))
            elif (
                HOR[ind] == 3
                and np.max(react_stoic[ind, np.where(this_orders == 3)]) == 3
            ):
                HOR[ind] = -3  # g_i should be(3 + 1/(x_i-1) + 2/(x_i-2))
    return HOR


# @njit(nogil=True, cache=False)
def tau_adaptive(
    react_stoic: np.ndarray,
    prod_stoic: np.ndarray,
    init_state: np.ndarray,
    k_det: np.ndarray,
    nc: int,
    eps: float,
    max_t: float,
    max_iter: int,
    volume: float,
    seed: int,
    chem_flag: bool,
) -> Tuple[np.ndarray, np.ndarray, int]:
    """
        Parameters
        ---------
        react_stoic : (ns, nr) ndarray
            A 2D array of the stoichiometric coefficients of the reactants.
            Reactions are rows and species are columns.
        prod_stoic : (ns, nr) ndarray
            A 2D array of the stoichiometric coefficients of the products.
            Reactions are rows and species are columns.
        init_state : (ns,) ndarray
            A 1D array representing the initial state of the system.
        k_det : (nr,) ndarray
            A 1D array representing the deterministic rate constants of the
            system.
        tau : float
            The constant time step used to tau leaping.
        max_t : float
            The maximum simulation time to run the simulation for.
        volume : float
            The volume of the reactor vessel which is important for second
            and higher order reactions. Defaults to 1 arbitrary units.
        seed : int
            The seed for the numpy random generator used for the current run
            of the algorithm.
        chem_flag : bool
            If True, divide by Na while calculating stochastic rate constants.
            Defaults to False.

        Returns
        -------
        t : ndarray
            Numpy array of the times.
        x : ndarray
            Numpy array of the states of the system at times in in `t`.
        status : int
            Indicates the status of the simulation at exit.
            1 : Succesful completion, terminated when `max_iter` iterations reached.
            2 : Succesful completion, terminated when `max_t` crossed.
            3 : Succesful completion, terminated when all species went extinct.
            -1 : Failure, order greater than 3 detected.
            -2 : Failure, propensity zero without extinction.
            -3 : Negative species count encountered
    """
    epsilon = 0.03
    ite = 1  # Iteration counter
    t_curr = 0.0  # Time in seconds
    ns = react_stoic.shape[0]
    nr = react_stoic.shape[1]
    v = prod_stoic - react_stoic  # ns x nr
    x = np.zeros((max_iter, ns))
    t = np.zeros((max_iter))
    x[0, :] = init_state.copy()
    n_events = np.zeros((nr,), dtype=np.int32)
    np.random.seed(seed)  # Set the seed
    # Determine kstoc from kdet and the highest order or reactions
    prop = np.copy(
        get_kstoc(react_stoic, k_det, volume, chem_flag)
    )  # Vector of propensities
    kstoc = prop.copy()  # Stochastic rate constants
    prop_sum = np.sum(prop)

    # Determine the HOR vector. HOR(i) is the highest order of reaction
    # in which species S_i appears as a reactant.
    HOR = get_HOR(react_stoic)

    if np.sum(prop) < 1e-30:
        if np.sum(x[ite - 1, :]) > 1e-30:
            status = -2
            return t[:ite], x[:ite, :], status

    M = nr
    N = ns
    L = np.zeros(M)
    K = np.zeros(M)
    vis = np.zeros(M)
    react_species = np.where(np.sum(react_stoic, axis=1) > 0)[0]
    n_react_species = react_species.shape[0]
    mup = np.zeros(n_react_species)
    sigp = np.zeros(n_react_species)
    tau_num = np.zeros(n_react_species)

    while ite < max_iter:
        # 1. Determine critical reactions

        # Calculate the propensities
        prop = np.copy(kstoc)
        for ind1 in range(nr):
            for ind2 in range(ns):
                # prop = kstoc * product of (number raised to order)
                prop[ind1] *= np.power(x[ite - 1, ind2], react_stoic[ind2, ind1])
        prop_sum = np.sum(prop)
        if prop_sum < 1e-30:
            status = 3
            return t[:ite], x[:ite, :], status
        for ind in range(M):
            vis = v[:, ind]
            L[ind] = np.nanmin(x[ite - 1, vis < 0] / abs(vis[vis < 0]))
        # A reaction j is critical if Lj <nc. However criticality is
        # considered only for reactions with propensity greater than
        # 0 (`prop > 0`).
        crit = (L < nc) * (prop > 0)
        # To get the non-critical reactions, we use the bitwise not operator.
        not_crit = ~crit
        # 2. Generate candidate taup
        if np.sum(not_crit) == 0:
            taup = HIGH
        else:
            # Compute mu from eqn 32a and sig from eqn 32b
            for ind, species_index in enumerate(react_species):
                temp = v[species_index, not_crit] * prop[not_crit]
                mup[ind] = np.sum(temp)
                sigp[ind] = np.sum(v[species_index, not_crit] * temp)
                if HOR[species_index] > 0:
                    g = HOR[species_index]
                elif HOR[species_index] == -2:
                    if x[ite - 1, species_index] is not 1:
                        g = 1 + 2 / (x[ite - 1, species_index] - 1)
                    else:
                        g = 2
                elif HOR[species_index] == -3:
                    if x[ite - 1, species_index] not in [1, 2]:
                        g = (
                            3
                            + 1 / (x[ite - 1, species_index] - 1)
                            + 2 / (x[ite - 1, species_index] - 2)
                        )
                    else:
                        g = 3
                elif HOR[species_index] == -32:
                    if x[ite - 1, species_index] is not 1:
                        g = 3 / 2 * (2 + 1 / (x[ite - 1, species_index] - 1))
                    else:
                        g = 3
                tau_num[ind] = max(epsilon * x[ite - 1, species_index] / g, 1)
            taup = np.nanmin(
                np.concatenate([tau_num / abs(mup), np.power(tau_num, 2) / abs(sigp)])
            )
        # 3. For small taup, do SSA
        if taup < 10 / prop_sum:
            t_ssa, x_ssa, status = direct_naive(
                react_stoic,
                prod_stoic,
                x[ite - 1, :],
                k_det,
                max_t=max_t - t[ite - 1],
                max_iter=min(100, max_iter - ite),
                volume=volume,
                seed=seed,
                chem_flag=chem_flag,
            )
            len_simulation = len(t_ssa)
            t[ite : ite + len_simulation] = t_ssa
            x[ite : ite + len_simulation, :] = x_ssa
            ite += len_simulation
            if status == 3 or status == 2:
                return t, x, status
            continue

        # 4. Generate second candidate taupp
        taupp = 1 / prop_sum * np.log(1 / np.random.rand())

        # 5. Leap
        if taup < taupp:
            tau = taup
            for ind in range(M):
                if not_crit[ind]:
                    K[ind] = np.random.poisson(prop[ind] * tau)
                else:
                    K[ind] = 0
        else:
            tau = taupp
            # Identify the only critical reaction to fire
            # Send in xt to match signature of roulette_selection
            temp = prop.copy()
            temp[not_crit] = 0
            j_crit, _ = roulette_selection(temp, x[ite - 1, :])
            for ind in range(M):
                if not_crit[ind]:
                    K[ind] = np.random.poisson(prop[ind] * tau)
                elif ind == j_crit:
                    K[ind] = 1
                else:
                    K[ind] = 0

        # 6. Handle negatives

        # K.shape = M
        # v.shape = N, M
        x[ite, :] = x[ite - 1, :] + np.dot(v, K)
        t[ite] = t[ite - 1] + tau
        ite += 1

        # Exit conditions
        if t[ite] > max_t:
            status = 2
            return t[:ite], x[:ite, :], status
    status = 1
    return t[:ite], x[:ite, :], status
