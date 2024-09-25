Terraform module for tempo-worker-k8s

This is a Terraform module facilitating the deployment of tempo-worker-k8s charm, using the [Terraform juju provider](https://github.com/juju/terraform-provider-juju/). For more information, refer to the provider [documentation](https://registry.terraform.io/providers/juju/juju/latest/docs). 


## Requirements
This module requires a `juju` model to be available. Refer to the [usage section](#usage) below for more details.

## API

### Inputs
The module offers the following configurable inputs:

| Name | Type | Description | Required |
| - | - | - | - |
| `app_name`| string | Application name | False |
| `channel`| string | Channel that the charm is deployed from | False |
| `config`| map(any) | Map of the charm configuration options | False |
| `model_name`| string | Name of the model that the charm is deployed on | True |
| `revision`| number | Revision number of the charm name | False |
| `units`| number | Number of units to deploy | False |

### Outputs
Upon applied, the module exports the following outputs:

| Name | Description |
| - | - |
| `app_name`|  Application name |
| `provides`| Map of `provides` endpoints |
| `requires`|  Map of `requires` endpoints |

## Usage

TODO: Update Tempo HA Terraform module link 
> [!NOTE]
> This module is intended to be used only in conjunction with its counterpart, [Tempo coordinator module](https://github.com/canonical/tempo-coordinator-k8s-operator) and, when deployed in isolation, is not functional. 
> For the Tempo HA solution module deployment, check [Tempo HA module](https://github.com/canonical/observability)

Users should ensure that Terraform is aware of the `juju_model` dependency of the charm module.

To deploy this module with its needed dependency, you can run `terraform apply -var="model_name=<MODEL_NAME>" -auto-approve`