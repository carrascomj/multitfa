import numpy as np
from .posdef import isPD, nearestPD
from collections import OrderedDict
from six import iteritems
from .thermo_constants import RT


def cov2corr(covariance):
    """ Calculates correlation matrix from covariance matrix
    corr(i,j) = cov(i,j)/stdev(i) * stdev(j) 

    Arguments:
        covariance {np.ndarray} -- covariance matrix

    Returns:
        [np.ndarray] -- correlation matrix
    """

    stdev = np.sqrt(np.diag(covariance))
    outer_stdev = np.outer(stdev, stdev)
    correlation = covariance / outer_stdev
    correlation[covariance == 0] = 0

    return correlation


def findcorrelatedmets(covariance, metabolites):
    """[summary]

    Arguments:
        covariance {[type]} -- [description]
        metabolites {[type]} -- [description]

    Returns:
        [type] -- [description]
    """

    correlation_mat = cov2corr(covariance)

    # Check for nan in correlation matrix and keep track of them and the corresponding metabolites
    non_prob_ind, prob_ind, non_prob_mets, nan_mets = [], [], [], []
    for i in range(len(correlation_mat)):
        if not np.isnan(correlation_mat[:, i]).all():
            non_prob_ind.append(i)
            non_prob_mets.append(metabolites[i])
        else:
            prob_ind.append(i)
            nan_mets.append(metabolites[i])

    reduced_correlation = correlation_mat[:, non_prob_ind]
    reduced_correlation = reduced_correlation[non_prob_ind, :]

    reduced_cov = covariance[:, non_prob_ind]
    reduced_cov = reduced_cov[non_prob_ind, :]

    # removing metabolites with all zeros in correlation matrix
    zero_mets, final_mets, non_zero_ind = [], [], []
    for i in range(0, len(reduced_correlation)):
        if np.count_nonzero(reduced_correlation[:, i]) == 0:
            zero_mets.append(non_prob_mets[i])
        else:
            non_zero_ind.append(i)
            final_mets.append(non_prob_mets[i])

    final_correlation = reduced_correlation[:, non_zero_ind]
    final_correlation = final_correlation[non_zero_ind, :]

    final_cov = reduced_cov[:, non_zero_ind]
    final_cov = final_cov[non_zero_ind, :]

    # Find high variance metabolites
    ind_high_variances = list(np.where(np.sqrt(np.diag(final_cov)) > 25)[0])

    # Find indices that are highly correlated (corr > 0.7 | corr < -0.7) and check if they are same as high variance metabolites
    (
        new_ellipsoid_ind,
        old_ellipsoid_ind,
        no_ellipse_mets,
        new_ellipse_mets,
        old_ellipse_mets,
    ) = ([], [], [], [], [])

    for i in range(len(final_correlation)):
        if i in ind_high_variances:
            pos_corr = list(set(np.where(final_correlation[:, i] > 0.7)[0]))
            neg_corr = list(set(np.where(final_correlation[:, i] < -0.7)[0]))
            correlated_ind = pos_corr + neg_corr
            if len(correlated_ind) == 0:
                no_ellipse_mets.append(final_mets[i])

            if set(correlated_ind).intersection(set(ind_high_variances)) == set(
                correlated_ind
            ):
                new_ellipsoid_ind.append(i)
                new_ellipse_mets.append(final_mets[i])

            else:
                old_ellipsoid_ind.append(i)
                old_ellipse_mets.append(final_mets[i])

        else:
            old_ellipsoid_ind.append(i)
            old_ellipse_mets.append(final_mets[i])

    old_cov = final_cov[:, old_ellipsoid_ind]
    old_cov = old_cov[old_ellipsoid_ind, :]
    new_cov = final_cov[:, new_ellipsoid_ind]
    new_cov = new_cov[new_ellipsoid_ind, :]

    return (old_ellipse_mets, new_ellipse_mets, old_cov, new_cov)


def Exclude_quadratic(model):

    big_var_rxn = []
    for rxn in model.reactions:
        if rxn.id in model.Exclude_reactions:
            continue
        lb_conc, ub_conc, lb_form, ub_form = (0, 0, 0, 0)
        for met, stoic in iteritems(rxn.metabolites):
            if met.Kegg_id in ["C00080", "cpd00067"]:
                continue
            if stoic < 0:
                lb_conc += stoic * met.concentration_variable.ub
                ub_conc += stoic * met.concentration_variable.lb
                lb_form += stoic * met.compound_variable.lb
                ub_form += stoic * met.compound_variable.ub
            else:
                lb_conc += stoic * met.concentration_variable.lb
                ub_conc += stoic * met.concentration_variable.ub
                lb_form += stoic * met.compound_variable.lb
                ub_form += stoic * met.compound_variable.ub

        lb_delG_rxn = RT * lb_conc + lb_form + rxn.transport_delG + rxn.transform
        ub_delG_rxn = RT * ub_conc + ub_form + rxn.transport_delG + rxn.transform

        if abs(lb_delG_rxn - ub_delG_rxn) > 5000:
            big_var_rxn.append(rxn)

    high_var_mets = []
    for reaction in big_var_rxn:
        for metabolite in reaction.metabolites:
            if metabolite.std_dev > 50:
                high_var_mets.append(metabolite.id)
    return list(set(high_var_mets))


def correlated_pairs(model):

    stdevs = np.sqrt(np.diag(model.cov_dG))
    high_var_met_inds = np.where(stdevs > 10)[0]

    high_var_mets = []
    for ind in high_var_met_inds:
        high_var_mets.append(model.metabolites[ind])
    high_var_rxns = []
    for met in high_var_mets:
        high_var_rxns.append(met.reactions)
    high_var_rxns = frozenset.union(*high_var_rxns)
    print(len(high_var_rxns))

    big_var_rxn_int = {}
    not_int = {}
    for rxn in high_var_rxns:
        if rxn.id in model.Exclude_reactions:
            continue
        lb_conc, ub_conc, lb_form, ub_form = (0, 0, 0, 0)
        for met, stoic in iteritems(rxn.metabolites):
            if met.Kegg_id in ["C00080", "cpd00067"]:
                continue
            if stoic < 0:
                lb_conc += stoic * met.concentration_variable.ub
                ub_conc += stoic * met.concentration_variable.lb
                lb_form += stoic * met.compound_variable.lb
                ub_form += stoic * met.compound_variable.ub
            else:
                lb_conc += stoic * met.concentration_variable.lb
                ub_conc += stoic * met.concentration_variable.ub
                lb_form += stoic * met.compound_variable.lb
                ub_form += stoic * met.compound_variable.ub

        lb_delG_rxn = RT * lb_conc + lb_form + rxn.transport_delG + rxn.transform
        ub_delG_rxn = RT * ub_conc + ub_form + rxn.transport_delG + rxn.transform

        if abs(lb_delG_rxn - ub_delG_rxn) < 2000:
            big_var_rxn_int[rxn] = abs(lb_delG_rxn - ub_delG_rxn)
        else:
            not_int[rxn] = abs(lb_delG_rxn - ub_delG_rxn)

    return big_var_rxn_int, not_int
