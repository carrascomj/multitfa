from .reaction import thermo_reaction
import numpy as np
from ..util.thermo_constants import Vmax, K, RT
from ..util.constraints import (
    directionality,
    delG_indicator,
    concentration_exp,
    metabolite_variables,
    reaction_variables,
    formation_exp,
    bounds_ellipsoid,
    quad_constraint,
)
from copy import deepcopy, copy
from ..util.dGf_calculation import calculate_dGf, cholesky_decomposition
from ..util.posdef import isPD, nearestPD
from ..util.util_func import Exclude_quadratic, correlated_pairs, quadratic_matrices
from .compound import Thermo_met
from warnings import warn
from six import iteritems
from cobra import Model
from .solution import get_solution, get_legacy_solution
from cobra.core.dictlist import DictList
import optlang
import itertools
import os
from random import choices
import string
from scipy import stats, linalg

present_dir = os.path.normpath(os.path.dirname(os.path.abspath(__file__)))


class tmodel(Model):
    def __init__(
        self,
        model,
        Kegg_map={},
        Exclude_list=[],
        pH_I_dict={},
        concentration_dict={"min": {}, "max": {}},
        tolerance_integral=1e-9,
        del_psi_dict={},
        correlated_met_pairs={},
        debug=False,
    ):
        """ Class representation of tMFA model, dependeds on cobra model class.
        
        Arguments:
            model {Cobra model object} -- Cobra model 
        
        Keyword Arguments:
            Kegg_map {dict} -- Dictionary of metabolite id to Kegg/seed identifiers of all metabolites in the model (default: {{}})
            
            Exclude_list {list} -- Reaction ids that needs to be excluded from thermodynamic analysis (e.g: exchange/sinks)  (default: {[]})
            
            pH_I_dict {dict} -- Dictionary of pH, ionic strength of different compartments, parameters for all the compartments needs to be specified  (default: {{}})
            
            concentration_dict {dict} -- Dictionary of min/max concentrations of metabolites where available (default: {{'min':{},'max':{}}})
            
            tolerance_integral {float} -- Integral tolerance of the solver (We recommend 1e-9 for tmfa problems) (default: {1e-9})
            
            del_psi_dict {dict} -- membrane potential dictionary for all the compartments in the model (default: {{}})
            
            debug {bool} -- Debugging flag (default: {False})
        """

        do_not_copy_by_ref = {
            "metabolites",
            "reactions",
            "genes",
            "notes",
            "annotation",
        }
        for attr in model.__dict__:
            if attr not in do_not_copy_by_ref:
                self.__dict__[attr] = model.__dict__[attr]

        self.metabolites = DictList()
        do_not_copy_by_ref = {"_reaction", "_model"}
        for metabolite in model.metabolites:
            new_met = Thermo_met(
                metabolite=metabolite,
                updated_model=self,
                Kegg_map=Kegg_map,
                concentration_dict=concentration_dict,
            )
            self.metabolites.append(new_met)

        self.genes = DictList()
        for gene in model.genes:
            new_gene = gene.__class__(None)
            for attr, value in iteritems(gene.__dict__):
                if attr not in do_not_copy_by_ref:
                    new_gene.__dict__[attr] = (
                        copy(value) if attr == "formula" else value
                    )
            new_gene._model = self
            self.genes.append(new_gene)

        self.reactions = DictList()
        do_not_copy_by_ref = {"_model", "_metabolites", "_genes"}
        for reaction in model.reactions:
            new_reaction = thermo_reaction(
                cobra_rxn=reaction,
                updated_model=self,
                Kegg_map=Kegg_map,
                pH_I_dict=pH_I_dict,
                del_psi_dict=del_psi_dict,
            )
            self.reactions.append(new_reaction)

        try:
            self._solver = deepcopy(model.solver)
            # Cplex has an issue with deep copies
        except Exception:  # pragma: no cover
            self._solver = copy(model.solver)  # pragma: no cover

        self.Kegg_map = Kegg_map
        self.Exclude_list = Exclude_list
        self.pH_I_dict = pH_I_dict
        self.concentration_dict = concentration_dict
        self.del_psi_dict = del_psi_dict
        self.covariance_matrix()
        self.solver.configuration.tolerances.integrality = tolerance_integral
        self.Exclude_reactions = list(
            set(Exclude_list + self.problematic_rxns)
        )  # See if we can make this a cached property
        self.correlated_met_pairs = correlated_met_pairs
        self.update_thermo_variables()
        self.update()

    @property
    def cholskey_matrix(self):
        """Calculates cholesky matrix (square root of covariance matrix of compounds)
        
        Returns:
            np.ndarray 
        """
        try:
            return self._cholskey_matrix
        except AttributeError:
            std_dg, cov_dg = calculate_dGf(self.metabolites, self.Kegg_map)
            self._cholskey_matrix = cholesky_decomposition(std_dg, cov_dg)

            return self._cholskey_matrix

    @property
    def gurobi_interface(self):
        try:
            return self._gurobi_interface
        except AttributeError:
            if self.solver.__class__.__module__ == "optlang.gurobi_interface":
                # self._gurobi_interface = self.solver.problem.copy()
                self._gurobi_interface = self.Quadratic_constraint()
                return self._gurobi_interface

    @property
    def cplex_interface(self):
        try:
            return self._cplex_interface
        except AttributeError:
            if self.solver.__class__.__module__ == "optlang.cplex_interface":
                self._cplex_interface = self.Quadratic_constraint()
                return self._cplex_interface

    @property
    def problem_metabolites(self):
        """ Metabolites for which we can't calculate the Gibbs free energy of formation using component contribution method

        Metabolites are considered problematic metabolites if whole row in cholesky matrix is zero
        
        Returns:
            List -- List of problematic metabolites
        """

        problematic_metabolites = []

        for met in self.metabolites:
            met_index = self.metabolites.index(met)
            if met.Kegg_id in ["C00080", "cpd00067"]:
                continue
            if np.count_nonzero(self.cholskey_matrix[:, met_index]) == 0 or np.isnan(
                met.delG_f
            ):  # or sqrt(diag(self.cov_dG)[self.metabolites.index(met)]) > 100:
                problematic_metabolites.append(met)

        return problematic_metabolites

    @property
    def problematic_rxns(self):
        """ Reactions which can't be included in thermodynamic analysis
            reactions involving problematic metabolites
        
        Returns:
            List -- List of reactions excluded, this combined with 'Exclude_list' gives us 'Exclude_reactions'
        """

        problematic_rxns = []
        for met in self.problem_metabolites:
            problematic_rxns.append(met.reactions)

        if len(problematic_rxns) > 0:
            problematic_rxns = frozenset.union(*problematic_rxns)
            problems = [i.id for i in problematic_rxns]
            return problems
        else:
            return []

    @property
    def correlated_metabolites(self):
        try:
            return self._correlated_metabolites
        except AttributeError:
            self._correlated_metabolites = correlated_pairs(self)
            return self._correlated_metabolites

    def covariance_matrix(self):
        """calculates the covariance matrix of Gibbs free energy of the metabolites
        
        Returns:
            std_dg [np.ndarray] -- std delg of all metabolites
            cov_dg [np.ndarray] -- covariance matrix
        """

        self.std_dG, self.cov_dG = calculate_dGf(self.metabolites, self.Kegg_map)

    def update_thermo_variables(self):
        """ Generates reaction and metabolite variables required for thermodynamic analysis and adds to the model

        Variables generated --

        Metabolite concentration variable
        Metabolite confidence variable
        Reaction delG variable
        Reaction flux indicator variable
        """
        # add formation energy variables w.r.t to kegg ids
        # metabolite concentration, Ci and metabolite variables
        conc_variables = []
        met_variables = []
        added_met_var = []

        for metabolite in self.metabolites:
            conc_var, met_variable = metabolite_variables(metabolite)
            conc_variables.append(conc_var)
            if metabolite.Kegg_id in added_met_var:
                continue
            met_variables.append(met_variable)
            added_met_var.append(metabolite.Kegg_id)
        self.add_cons_vars(conc_variables + met_variables)

        # Now add reaction variables and generate remaining constraints
        for rxn in self.reactions:
            if rxn.id in self.Exclude_reactions:
                continue
            reaction_vars = reaction_variables(rxn)
            self.add_cons_vars(reaction_vars)

    def calculate_S_matrix(self):
        """ Calculates the stoichiometric matrix (metabolites * Reactions)

        Returns:
            Tuple  -- Tuple of reaction order, np.ndarray of stoichiometric matrix
        """

        n_reactions = len(self.reactions)
        n_metabolites = len(self.metabolites)
        S_matrix = np.zeros((2 * n_reactions, n_metabolites))

        reaction_index = 0
        rxn_order = []
        for reaction in self.reactions:
            rxn_order.append(reaction.forward_variable.name)
            rxn_order.append(reaction.reverse_variable.name)
            for metabolite, stoic in iteritems(reaction.metabolites):
                S_matrix[reaction_index, self.metabolites.index(metabolite)] = stoic
                S_matrix[
                    reaction_index + 1, self.metabolites.index(metabolite)
                ] = -stoic
            reaction_index = reaction_index + 2

        S = np.transpose(S_matrix)

        return rxn_order, S

    def _generate_constraints(self):
        """ Generates thermodynamic constraints for the model. See util/constraints.py for detailed explanation of constraints

        Vi - Vmax * Zi <= 0
        delGr - K + K * Zi <= 0
        delGr - RT * S.T * ln(x) - S.T @ delGf - delGtransport = 0

        
        Returns:
            List -- List of themrodynamic constraints
        """
        # if self.correlated_metabolites == None:
        correlated_mets = correlated_pairs(self)

        # Add covariance constraint to the correlated metabolite pairs
        for met in correlated_mets:
            primary_met = self.metabolites.get_by_id(met)
            ind_met = self.metabolites.index(primary_met)
            for i in range(len(correlated_mets[met])):
                paired_met_id = list(correlated_mets[met])[i]
                paired_met = self.metabolites.get_by_id(paired_met_id)
                if primary_met.Kegg_id == paired_met.Kegg_id:
                    continue
                ind_paired_met = self.metabolites.index(paired_met)
                covar_pair = self.cov_dG[ind_met, ind_paired_met]
                var_difference = (
                    primary_met.std_dev ** 2 + paired_met.std_dev ** 2 - 2 * covar_pair
                )
                constraint_covar_lb = self.problem.Constraint(
                    primary_met.compound_variable - paired_met.compound_variable,
                    lb=-1.96 * np.sqrt(var_difference),
                    name="covar_{}_{}_lb".format(primary_met.id, paired_met.id),
                )
                constraint_covar_ub = self.problem.Constraint(
                    primary_met.compound_variable - paired_met.compound_variable,
                    ub=1.96 * np.sqrt(var_difference),
                    name="covar_{}_{}_ub".format(primary_met.id, paired_met.id),
                )
                self.add_cons_vars([constraint_covar_lb, constraint_covar_ub])

        rxn_constraints = []
        # Now add reaction variables and generate remaining constraints
        for rxn in self.reactions:
            if rxn.id in self.Exclude_reactions:
                continue

            # Directionality constraint
            dir_f, dir_r = directionality(rxn)
            ind_f, ind_r = delG_indicator(rxn)

            # delG constraint
            concentration_term = concentration_exp(rxn)
            met_term = formation_exp(rxn)

            lhs_forward = rxn.delG_forward - RT * concentration_term - met_term
            lhs_reverse = rxn.delG_reverse + RT * concentration_term + met_term
            rhs = rxn.delG_transform

            delG_f = self.problem.Constraint(
                lhs_forward,
                lb=rhs,
                ub=rhs,
                name="delG_{}".format(rxn.forward_variable.name),
            )

            delG_r = self.problem.Constraint(
                lhs_reverse,
                lb=-rhs,
                ub=-rhs,
                name="delG_{}".format(rxn.reverse_variable.name),
            )
            rxn_constraints.extend([dir_f, dir_r, ind_f, ind_r, delG_f, delG_r])

        return rxn_constraints

    def update(self):
        """ Adds the generated thermo constaints to  model. Checks for duplication 
        """
        constraints = self._generate_constraints()
        for cons in constraints:
            if cons.name not in self.constraints:
                self.add_cons_vars([cons])
            else:
                warn(
                    "Constraint {} already in the model, removing previous entry".format(
                        cons.name
                    )
                )
                self.solver.remove(cons.name)
                self.add_cons_vars([cons])

    def optimize(self, solve_method="MIQC", raise_error=False):
        """ solves the model with given constraints. By default, we try to solve the model with quadratic constraints. Note: Quadratic constraints are supported by Gurobi/Cplex currently. if either of two solvers are not found, one can solve 'box' type MILP problem.

        :param solve_method: Method to solve the problem, defaults to "MIQC", any other string input leades to solving with box MILP  
        :type solve_method: str, optional
        :param raise_error: , defaults to False
        :type raise_error: bool, optional
        :return: returns solution object
        :rtype: solution object (refer to Solution class)
        """

        if solve_method == "MIQC":
            if not (
                optlang.available_solvers["GUROBI"]
                or optlang.available_solvers["CPLEX"]
            ):
                warn(
                    "GUROBI/CPLEX not available, Quadratic constraints are not supported by current solver"
                )
                return

            if self.solver.__class__.__module__ == "optlang.gurobi_interface":
                self.gurobi_interface.optimize()
                solution = get_legacy_solution(self, solver="gurobi")

                return solution

            elif self.solver.__class__.__module__ == "optlang.cplex_interface":
                solution = self.cplex_interface.solve()
                solution = get_legacy_solution(self, solver="cplex")

                return solution

        else:
            self.slim_optimize()
            solution = get_solution(self, raise_error=raise_error)

            return solution

    def concentration_ratio_constraints(self, ratio_metabolites, ratio_lb, ratio_ub):
        """ Function to add metabolite concentration ratio constraints to the model. E.g. ratio of redox pairs
        
        Arguments:
            ratio_metabolites {Tuple} -- Tuple of metabolite names to which we have concentration ratios in the same order

            ratio_lb {List} -- Lower bound of the ratio
            ratio_ub {List} -- Upper bound on the ratio
        """

        for i in range(len(ratio_metabolites)):
            ratio_met1 = self.metabolites.get_by_id(ratio_metabolites[i][0])

            ratio_met2 = self.metabolites.get_by_id(ratio_metabolites[i][1])

            ratio_constraint = self.problem.Constraint(
                1 * ratio_met1.concentration_variable
                - 1 * ratio_met2.concentration_variable,
                lb=ratio_lb[i],
                ub=ratio_ub[i],
            )

            self.add_cons_vars(ratio_constraint)

    def Quadratic_constraint(self):
        """ Adds Quadratic constraint to the model's Gurobi/Cplex Interface. 
        (x-mu).T @ inv(cov) @ (x-mu) <= chi-square
        Note: This one creates one ellipsoidal constraint for all the metabolites that has non zero or non 'nan' formation energy, irrespective of the magnitude of variance. if the model is infeasible after adding this constraint, refer to util_func.py, find_correlated metabolites to add different ellipsoidal constraints to high variance and normal compounds to avoid possible numerical issues.
        
        Unable to retrieve quadratic constraints in Gurobi model, can see the QC when printed.

        :raises NotImplementedError: Implemented only for Gurobi/Cplex interfaces.
        :return: [description]
        :rtype: [type]
        """
        correlated_mets = correlated_pairs(self)
        correlated_met_values = list(itertools.chain(*correlated_mets.values()))

        # Problem metabolites, if met.delGf == 0 or cholesky row is zeros then delete them
        delete_met, cov_mets, cov_met_inds, non_duplicate = [], [], [], []

        # Identify problematic high variance metabolites
        #  Find metabolites that are present in different compartments and make sure they get one row/col in covariance matrix
        for met in self.metabolites:
            if (
                met.delG_f == 0
                or np.isnan(met.delG_f)
                or met.std_dev > 50
                or met.id in correlated_met_values
                or met.id in correlated_mets.keys()
            ):
                delete_met.append(met)
            else:
                if met.Kegg_id in non_duplicate:
                    continue
                else:
                    cov_met_inds.append(self.metabolites.index(met))
                    cov_mets.append(met)
                    non_duplicate.append(met.Kegg_id)

        # Pick indices of non zero non nan metabolites
        cov_dg = self.cov_dG[:, cov_met_inds]
        cov_dg = cov_dg[cov_met_inds, :]

        # Now calculate cholesky of the cov_dg
        chol_cov_dg = np.linalg.cholesky(cov_dg)
        chisq_val = stats.chi2.isf(q=0.05, df=len(cov_dg))

        # Calculate ellipsoid box bounds and set to variables
        bounds = bounds_ellipsoid(cov_dg)  # Check for posdef cov_dg

        if self.solver.__class__.__module__ == "optlang.gurobi_interface":
            from gurobipy import QuadExpr

            solver_interface = self.solver.problem.copy()

            sphere_vars = []
            for metabolite in cov_mets:
                solver_interface.addVar(
                    lb=-1, ub=1, name="Sphere_{}".format(metabolite.Kegg_id)
                )

                # Update the formation error variables to represent the eigen value vactor pair
                solver_interface.getVarByName(
                    "met_{}".format(metabolite.Kegg_id)
                ).LB = -bounds[cov_mets.index(metabolite)]

                solver_interface.getVarByName(
                    "met_{}".format(metabolite.Kegg_id)
                ).UB = bounds[cov_mets.index(metabolite)]

            solver_interface.update()

            sphere_vars = [
                solver_interface.getVarByName("Sphere_{}".format(met.Kegg_id))
                for met in cov_mets
            ]

            sphere_vars_np = np.array(sphere_vars)
            sphere_vars_np = sphere_vars_np[np.newaxis, :]

            # Add the spherical quadratic constraint
            lhs_q_expr = QuadExpr()
            lhs_q_expr.addTerms(
                len(sphere_vars) * [1], list(sphere_vars), list(sphere_vars)
            )
            solver_interface.addQConstr(lhs_q_expr <= 1, name="Quadratic")

            # Now define relation between ellipse error vars and sphere vars
            ellipse_error_vars = [
                solver_interface.getVarByName("met_{}".format(met.Kegg_id))
                for met in cov_mets
            ]

            sph_relation = np.sqrt(chisq_val) * chol_cov_dg @ sphere_vars_np.T

            for i in range(len(ellipse_error_vars)):
                # print(ellipse_error_vars[i].VarName)
                lhs_expr = ellipse_error_vars[i] - sph_relation[i][0]
                varname = "relation_{}".format(ellipse_error_vars[i].VarName)
                # print(lhs_expr, varname)
                solver_interface.addConstr(
                    lhs_expr == 0, name=varname,
                )
            solver_interface.update()

            return solver_interface

        elif self.solver.__class__.__module__ == "optlang.cplex_interface":

            from cplex import Cplex, SparseTriple, SparsePair

            tmp_dir = present_dir + os.sep + os.pardir + os.sep + "tmp"

            if not os.path.exists(tmp_dir):
                os.makedirs(tmp_dir)

            rand_str = "".join(choices(string.ascii_lowercase + string.digits, k=6))
            # write cplex model to mps file and re read
            self.solver.problem.write(tmp_dir + os.sep + rand_str + ".mps")
            # Instantiate Cplex model
            cplex_model = Cplex()
            cplex_model.read(tmp_dir + os.sep + rand_str + ".mps")

            Sphere_var_names = []
            for met in cov_mets:
                # Adjust the formation error variable bounds with eigen value, vector pair
                err_var_name = "met_{}".format(met.Kegg_id)
                cplex_model.variables.set_lower_bounds(
                    err_var_name, -bounds[cov_mets.index(met)]
                )
                cplex_model.variables.set_upper_bounds(
                    err_var_name, bounds[cov_mets.index(met)]
                )

                # Now define the variables for spherical variables for cov_mets
                Sphere_var_names.append("Sphere_{}".format(met.Kegg_id))

            cplex_model.variables.add(
                names=Sphere_var_names,
                lb=len(Sphere_var_names) * [-1],
                ub=len(Sphere_var_names) * [1],
            )

            # Add the Sphere constraint
            cplex_model.quadratic_constraints.add(
                quad_expr=SparseTriple(
                    ind1=Sphere_var_names,
                    ind2=Sphere_var_names,
                    val=len(Sphere_var_names) * [1],
                ),
                rhs=1,
                name="ellipse",
            )

            # Add the linear constraints to draw relation between formation error variables and the sphere variables using cholesky matrix
            ellipse_error_vars = ["met_{}".format(met.Kegg_id) for met in cov_mets]

            for i in range(len(ellipse_error_vars)):
                var_names = [ellipse_error_vars[i]]
                var_names.extend(Sphere_var_names)
                coeffs = [1]
                coeffs.extend(list(-np.sqrt(chisq_val) * chol_cov_dg[i, :]))
                cons_name = "relation_{}".format(ellipse_error_vars[i])
                indices = cplex_model.linear_constraints.add(
                    lin_expr=[SparsePair(ind=var_names, val=coeffs)],
                    senses="E",
                    rhs=[0],
                    names=[cons_name],
                )

            return cplex_model

        else:
            raise NotImplementedError("Current solver doesn't support QC")

    def export_MIP_matrix(self):
        """ Creates matrices structure of the MILP problem. Quadratic constraint is not exported.

        :return: lhs- lhs matrix representing all constraints
                rhs - rhs matrix
                var_names - variable name
                lb, ub- lower, upper bounds of variables
                cons_sense - constraint sense (eg: equal, less equal etc)

        :rtype: Tuple
        """

        rxn_var, S = self.calculate_S_matrix()
        n_mets, rxn_tot = np.shape(S)
        n_rxn_Excl = len(self.Exclude_reactions)
        n_core_rxns = rxn_tot - n_rxn_Excl

        # Create S.v = 0 and expand for other constraints
        mass_balance = np.concatenate(
            (S, np.zeros((n_mets, 2 * n_core_rxns + 2 * n_mets))), axis=1
        )
        rhs_mass_bal = [0] * n_mets
        sense_mass_bal = ["E"] * n_mets

        indicators, delGr, concentration, formation, rhs_delG, sense = (
            [],
            [],
            [],
            [],
            [],
            [],
        )
        # Intialize 6 constraints
        delG_cons_matrix = np.zeros((2 * n_core_rxns, 3 * n_core_rxns + 2 * n_mets))
        for reaction in self.reactions:
            if reaction.id in self.Exclude_reactions:
                continue
            S_rxn = reaction.S_matrix
            rxn_index = rxn_var.index(reaction.id)
            # vi-vmax*zi <= 0
            delG_cons_matrix[rxn_index, rxn_index] = 1
            delG_cons_matrix[rxn_index + 1, rxn_index] = 1
            delG_cons_matrix[rxn_index, rxn_index + n_core_rxns] = -Vmax
            delG_cons_matrix[rxn_index + 1, rxn_index + n_core_rxns] = -Vmax
            # delG - k*zi <= k
            delG_cons_matrix[rxn_index + 2, 2 * n_core_rxns + rxn_index] = 1
            delG_cons_matrix[rxn_index + 3, 2 * n_core_rxns + rxn_index] = 1
            delG_cons_matrix[rxn_index + 2, n_core_rxns + rxn_index] = -K
            delG_cons_matrix[rxn_index + 3, n_core_rxns + rxn_index] = -K
            # delG - s.T RT ln(x) - S.T delGf = 0
            delG_cons_matrix[rxn_index + 4, 2 * n_core_rxns + rxn_index] = 1
            delG_cons_matrix[rxn_index + 5, 2 * n_core_rxns + rxn_index] = 1
            delG_cons_matrix[
                rxn_index + 4, 3 * n_core_rxns : 3 * n_core_rxns + n_mets
            ] = -S_rxn.T
            delG_cons_matrix[
                rxn_index + 5, 3 * n_core_rxns : 3 * n_core_rxns + n_mets
            ] = S_rxn.T
            delG_cons_matrix[
                rxn_index + 4, 3 * n_core_rxns + n_mets : 3 * n_core_rxns + 2 * n_mets
            ] = -S_rxn.T
            delG_cons_matrix[
                rxn_index + 5, 3 * n_core_rxns + n_mets : 3 * n_core_rxns + 2 * n_mets
            ] = S_rxn.T

            indicators.extend(
                [reaction.indicator_forward.name, reaction.indicator_reverse.name]
            )
            delGr.extend([reaction.delG_forward.name, reaction.delG_reverse.name])
            rhs_delG.extend([0, K, 0])
            sense.extend(["L", "L", "E"])

        lb_conc, lb_formation, ub_conc, ub_formation = ([], [], [], [])
        for met in self.metabolites:
            concentration.append(met.concentration_variable.name)
            formation.append(met.compound_variable.name)
            lb_conc.append(met.concentration_variable.lb)
            ub_conc.append(met.concentration_variable.ub)
            lb_formation.append(met.compound_variable.lb)
            ub_formation.append(met.compound_variable.ub)

        var_names = rxn_var + indicators + delGr + concentration + formation

        lhs = np.concatenate((mass_balance, delG_cons_matrix), axis=0)
        rhs = rhs_mass_bal + rhs_delG
        cons_sense = sense_mass_bal + sense
        lb = (
            [-1000] * rxn_tot
            + [0] * n_core_rxns
            + [-1e5] * n_core_rxns
            + lb_conc
            + lb_formation
        )
        ub = (
            [1000] * rxn_tot
            + [1] * n_core_rxns
            + [1e5] * n_core_rxns
            + ub_conc
            + ub_formation
        )

        return (lhs, rhs, var_names, np.array(lb), np.array(ub), cons_sense)

