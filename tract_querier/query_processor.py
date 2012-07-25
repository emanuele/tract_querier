import ast
from os import path
from copy import deepcopy
from itertools import takewhile
from collections import Counter
import fnmatch


keywords = [
    'and',
    'or',
    'not in',
    'not',
    'only',
    'endpoints_in'
]


class TractQuerierSyntaxError(ValueError):
    def __init__(self, value):
        self.value = value
    def __str__(self):
        return repr(self.value)


class RewriteChangeNotInPrescedence(ast.NodeTransformer):
    def visit_BoolOp(self, node):
        predicate = lambda value: not (
                isinstance(value, ast.Compare) and
                isinstance(value.ops[0], ast.NotIn)
        )

        values_which_are_not_in_op = [value for value in takewhile(
            predicate,
            node.values[1:]
        )]

        if (len(values_which_are_not_in_op) == len(node.values) - 1):
            return node

        old_CompareNode = node.values[len(values_which_are_not_in_op) + 1]
        new_CompareNodeLeft = ast.copy_location(
            ast.BoolOp(
                op=node.op,
                values=(
                    [node.values[0]] +
                    values_which_are_not_in_op +
                    [old_CompareNode.left]
                )
            ),
            node
        )

        new_CompareNode = ast.copy_location(
            ast.Compare(
                left=new_CompareNodeLeft,
                ops=old_CompareNode.ops,
                comparators=old_CompareNode.comparators
            ),
            node
        )

        rest_of_the_values = node.values[len(values_which_are_not_in_op) + 2:]

        if len(rest_of_the_values) == 0:
            return self.visit(new_CompareNode)
        else:
            return self.visit(ast.copy_location(
                ast.BoolOp(
                    op=node.op,
                    values=(
                        [new_CompareNode] +
                        rest_of_the_values
                                        )
                ),
                node
            ))


class RewriteSide(ast.NodeTransformer):
    def __init__(self, left=True):
        if left:
            self.side = 'left'
            self.opposite = 'right'
        else:
            self.side = 'right'
            self.opposite = 'left'

    def visit_Attribute(self, node):
        if node.attr == 'side':
            return ast.copy_location(
                ast.Name(id=node.value.id + '_' + self.side),
                node
            )
        elif node.attr == 'opposite':
            return ast.copy_location(
                ast.Name(id=node.value.id + '_' + self.opposite),
                node
            )
        else:
            raise TractQuerierSyntaxError(
                "Invalid subscript: " + ast.dump(node)
            )

class RewritePreprocess(ast.NodeTransformer):
    def __init__(self, *args, **kwargs):
        if 'include_folders' in kwargs:
            self.include_folders = kwargs['include_folders']
            kwargs['include_folders'] = None
            del kwargs['include_folders']
        else:
            self.include_folders = ['.']
        super(RewritePreprocess, self).__init__(*args, **kwargs)


    rewrite_left = RewriteSide(left=True)
    rewrite_right = RewriteSide(left=False)

    def visit_Attribute(self, node):
        if node.attr == 'left':
            new_node = ast.Name(id=node.value.id + '_left')
        elif node.attr == 'right':
            new_node = ast.Name(id=node.value.id + '_right')
        elif node.attr == 'side' or node.attr == 'opposite':
            new_node = ast.Module([
                self.rewrite_left.visit(deepcopy(node)),
                self.rewrite_right.visit(deepcopy(node))
            ])

        return ast.copy_location(
            self.visit(new_node),
            node
        )

    def visit_Assign(self, node):
        if isinstance(node.targets[0], ast.Attribute):
            if node.targets[0].attr != 'side':
                raise TractQuerierSyntaxError("Wrong attribute")

            node_left_right = ast.Module([
                self.rewrite_left.visit(deepcopy(node)),
                self.rewrite_right.visit(deepcopy(node))
            ])

            return ast.copy_location(
                self.visit(node_left_right),
                node
            )
        else:
            return ast.copy_location(
                ast.Assign(
                    targets=[self.visit(t) for t in node.targets],
                    value=self.visit(node.value)
                )
            , node)


    def visit_AugAssign(self, node):
        if isinstance(node.target, ast.Attribute):
            if node.target.attr != 'side':
                raise TractQuerierSyntaxError("Wrong attribute")

            node_left_right = ast.Module([
                self.rewrite_left.visit(deepcopy(node)),
                self.rewrite_right.visit(deepcopy(node))
            ])

            return ast.copy_location(
                self.visit(node_left_right),
                node
            )
        else:
            return ast.copy_location(
                ast.AugAssign(
                    target=self.visit(node.target),
                    value=self.visit(node.value),
                    op=node.op
                )
            , node)

    def visit_Name(self, node):
        return ast.copy_location(
            ast.Name(id=node.id.lower()),
            node
        )

    def visit_Str(self, node):
        return ast.copy_location(
            ast.Str(s=node.s.lower()),
            node
        )


    def visit_Import(self, node):
        try:
            module_names = []
            for module_name in node.names:
                file_name = module_name.name
                found = False
                for folder in self.include_folders:
                    file_ = path.join(folder, file_name)
                    if path.exists(file_) and path.isfile(file_):
                        module_names.append(file_)
                        found =True
                        break
                if not found:
                    raise TractQuerierSyntaxError(
                        'Imported file not found: %s' % file_name
                    )
            imported_modules = [
                ast.parse(file(module_name).read(), filename=module_name)
                for module_name in module_names
            ]
        except SyntaxError:
            import sys
            import traceback
            exc_type, exc_value, exc_traceback = sys.exc_info()
            formatted_lines = traceback.format_exc().splitlines()
            raise TractQuerierSyntaxError('syntax error in line %s line %d: \n%s\n%s' %
                                          (
                                              module_name,
                                              exc_value[1][1],
                                              formatted_lines[-3],
                                              formatted_lines[-2]
                                          )
                                         )


        new_node = ast.Module(imported_modules)

        return ast.copy_location(
            self.visit(new_node),
            node
        )


class EvaluateQueries(ast.NodeVisitor):
    def __init__(self,
                 crossing_fibers_labels, crossing_labels_fibers,
                 ending_fibers_labels, ending_labels_fibers
                ):
        self.crossing_fibers_labels = crossing_fibers_labels
        self.crossing_labels_fibers = crossing_labels_fibers

        self.ending_fibers_labels = ending_fibers_labels
        self.ending_labels_fibers = ending_labels_fibers

        self.evaluated_queries_fibers = {}
        self.evaluated_queries_labels = {}
        self.queries_to_save = set()

    def visit_Module(self, node):
        for line in node.body:
            self.visit(line)

    def visit_Compare(self, node):
        if any(not isinstance(op, ast.NotIn) for op in node.ops):
            raise TractQuerierSyntaxError("Invalid syntax in query line %d" % node.lineno)

        fibers, labels = self.visit(node.left)
        fibers = fibers.copy()
        labels = labels.copy()
        for value in node.comparators:
            fibers_, labels_ = self.visit(value)
            fibers.difference_update(fibers_)
            labels.difference_update(labels_)

        return fibers, labels

    def visit_BoolOp(self, node):
        fibers, labels = self.visit(node.values[0])
        fibers = fibers.copy()
        labels = labels.copy()

        if isinstance(node.op, ast.Or):
            for value in node.values[1:]:
                fibers_, labels_ = self.visit(value)
                fibers.update(fibers_)
                labels.update(labels_)

        elif isinstance(node.op, ast.And):
            for value in node.values[1:]:
                    fibers_, labels_ = self.visit(value)
                    fibers.intersection_update(fibers_)
                    labels.update(labels_)

        else:
            return self.generic_visit(node)

        return fibers, labels

    def visit_BinOp(self, node):
        fibers_left, label_left = self.visit(node.left)
        fibers_right,label_right = self.visit(node.right)
        if isinstance(node.op, ast.Add):
            return fibers_left | fibers_right, label_left | label_right
        if isinstance(node.op, ast.Mult):
            return fibers_left & fibers_right, label_left | label_right
        if isinstance(node.op, ast.Sub):
            return fibers_left.difference(fibers_right), label_left.difference(label_right)
        else:
            return self.generic_visit(node)

    def visit_UnaryOp(self, node):
        fibers, labels = self.visit(node.operand)
        if isinstance(node.op, ast.Invert):
            return set(fiber for fiber in fibers if self.crossing_fibers_labels[fiber].issubset(labels)), labels
        elif isinstance(node.op, ast.UAdd):
            return fibers, labels
        elif isinstance(node.op, ast.USub) or isinstance(node.op, ast.Not):
            all_labels = set(self.crossing_labels_fibers.keys())
            all_labels.difference_update(labels)
            fibers = set().union(*tuple((self.crossing_labels_fibers[label] for label in all_labels)))
            return fibers, all_labels
        else:
            raise TractQuerierSyntaxError("Syntax error in query line %d" % node.lineno)

    def visit_Str(self, node):
        matching_fibers = set()
        matching_labels = set()
        for name in self.evaluated_queries_fibers.keys():
            if fnmatch.fnmatch(node.s, name):
                matching_fibers.update(self.evaluated_queries_fibers[name])
                matching_labels.update(self.evaluated_queries_labels[name])

        return matching_fibers, matching_labels

    def visit_Call(self, node):

        if (#Single string argument function
            isinstance(node.func, ast.Name) and
            len(node.args) == 1 and
            len(node.args) == 1 and
            node.starargs is None and
            node.keywords == [] and
            node.kwargs is None
        ):
            if (node.func.id.lower() == 'only' ):
                fibers, labels = self.visit(node.args[0])
                return (
                    set(
                        fiber for fiber in fibers
                        if self.crossing_fibers_labels[fiber].issubset(labels)
                    ), labels
                )
            elif (node.func.id.lower() == 'endpoints_in'):
                fibers, labels = self.visit(node.args[0])
                fibers = set(
                        fiber for fiber in fibers
                        if (self.ending_fibers_labels[fiber].intersection(labels))
                    )
                labels = set().union(
                    *tuple((self.crossing_fibers_labels[fiber] for fiber in fibers))
                )
                return fibers, labels
            elif (node.func.id.lower() == 'save' and isinstance(node.args, ast.Str)):
                self.queries_to_save.add(node.args[0].s)
                return

        raise TractQuerierSyntaxError("Invalid query in line %d" %node.lineno)


    def visit_Assign(self, node):
        if len(node.targets) > 1 or not isinstance(node.targets[0], ast.Name):
            raise TractQuerierSyntaxError("Invalid assignment in line %d" % node.lineno)
        fibers, labels = self.visit(node.value)
        self.queries_to_save.add(node.targets[0].id)
        self.evaluated_queries_fibers[node.targets[0].id] = fibers
        self.evaluated_queries_labels[node.targets[0].id] = labels


    def visit_AugAssign(self, node):
        if not isinstance(node.op, ast.BitOr) or not isinstance(node.target, ast.Name):
            raise TractQuerierSyntaxError("Invalid assignment in line %d" % node.lineno)
        fibers, labels = self.visit(node.value)
        self.evaluated_queries_fibers[node.target.id] = fibers
        self.evaluated_queries_labels[node.target.id] = labels


    def visit_Name(self, node):
        if node.id in self.evaluated_queries_fibers:
            return self.evaluated_queries_fibers[node.id], self.evaluated_queries_labels[node.id]
        else:
            raise TractQuerierSyntaxError("Invalid query name in line %d: %s" % (node.lineno, node.id))


    def visit_Num(self, node):
        if node.n in self.crossing_labels_fibers:
            fibers = self.crossing_labels_fibers[node.n]
        else:
            fibers = set()
        return fibers, set((node.n,))

    def visit_Expr(self, node):
        if isinstance(node.value, ast.Name):
            if node.value.id in self.evaluated_queries_fibers.keys():
                self.queries_to_save.add(node.value.id)
            else:
                raise TractQuerierSyntaxError("Query %s not known line: %d" % (node.value.id, node.lineno))
        elif isinstance(node.value, ast.Module):
            self.visit(node.value)
        else:
            raise TractQuerierSyntaxError("Invalid expression at line: %d" % (node.lineno))

    def generic_visit(self, node):
        raise TractQuerierSyntaxError("Invalid Operation %s line: %d" % (type(node), node.lineno) )


def queries_preprocess(query_file, filename='<unknown>', include_folders=[]):

    try:
        query_file_module = ast.parse(query_file, filename='<unknown>')
    except SyntaxError:
        import sys
        import traceback
        exc_type, exc_value, exc_traceback = sys.exc_info()
        formatted_lines = traceback.format_exc().splitlines()
        raise TractQuerierSyntaxError(
            'syntax error in line %s line %d: \n%s\n%s' %
            (
                filename,
                exc_value[1][1],
                formatted_lines[-3],
                formatted_lines[-2]
            )
        )

    rewrite_preprocess = RewritePreprocess(include_folders=include_folders)
    rewrite_precedence_not_in = RewriteChangeNotInPrescedence()

    preprocessed_module = rewrite_precedence_not_in.visit(
        rewrite_preprocess.visit(query_file_module)
    )

    return preprocessed_module.body


def eval_queries(
    crossing_labels_fibers, crossing_fibers_labels,
    ending_labels_fibers, ending_fibers_labels,
    query_file_body
):
    eq = EvaluateQueries(
        crossing_fibers_labels, crossing_labels_fibers,
        ending_fibers_labels, ending_labels_fibers
    )
    if isinstance(query_file_body, list):
        eq.visit(ast.Module(query_file_body))
    else:
        eq.visit(query_file_body)

    return dict([(key, eq.evaluated_queries_fibers[key]) for key in eq.queries_to_save])


def queries_syntax_check(query_file_body):
    eval_queries({}, {}, {}, {}, query_file_body)


def labels_for_fibers(crossing_fibers_labels):
    crossing_labels_fibers = {}
    for i, f in crossing_fibers_labels.items():
        for l in f:
            if l in crossing_labels_fibers:
                crossing_labels_fibers[l].add(i)
            else:
                crossing_labels_fibers[l] = set((i,))
    return crossing_labels_fibers


